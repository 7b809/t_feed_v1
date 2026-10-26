# services/isolated_instrument_promotion_service.py
"""Promote the entry-signal winner to be the active isolated instrument.

Background
----------
The isolation engine (``level_isolated_instrument_service``) fires on
the first R3 / S3 touch and permanently binds itself to the **bias
side** instrument (CE for R3, PE for S3) for the day. It cannot be re-run.

The strategy-entry engine (``strategy_entry_signal_service``) evaluates
BOTH sides at the isolated strike (crossover). Whichever side's post-
touch support confirms first becomes ``chosen_instrument_key``.

When the chosen instrument is the **counter side** (e.g. PE bias, CE
wins), the two disagree: the official isolated instrument is PE, but
the signal says CE is the actual trade. This service resolves the
disagreement by recording a **promotion** — the counter-side
instrument becomes the "active" one that the SR tracker, hero card,
and snapshot card focus on. The isolation record itself is never
modified (it is the historical truth of what triggered at T_touch);
the original instrument is preserved inside the promotion sidecar
under ``record`` and the trigger_event stays intact in the isolation
record.

Sidecar file
------------
``<LIVE_DATA_DIR>/level_isolated_instrument_promotion.json``::

    {
      "day": "2026-10-26",
      "promoted_at": "2026-10-26T09:41:00+05:30",
      "from_instrument_key": "NSE_FO|51327",
      "from_trading_symbol": "NIFTY 22400 PE 27 OCT 26",
      "to_instrument_key": "NSE_FO|51326",
      "to_trading_symbol": "NIFTY 22400 CE 27 OCT 26",
      "reason": "strategy_entry_armed",
      "evidence": {
        "entry_support_price": 155.00,
        "entry_support_confirmed_time": "2026-10-26T09:41:00+05:30",
        "entry_ltp": 177.00,
        "entry_price_source": "candle_close_at_confirmed"
      },
      "record": {
        # Mirrors the shape of `isolated` but for the promoted
        # instrument, so the UI can swap records transparently.
        "instrument_key":        "NSE_FO|51326",
        "trading_symbol":        "NIFTY 22400 CE 27 OCT 26",
        "underlying":            "NIFTY",
        "strike":                22400,
        "type":                  "CE",
        "expiry":                "...",
        "reason":                "nifty_s3_touch",
        "trigger_level":         "S3",
        "trigger_level_value":   22415.70,
        "trigger_index":         "NIFTY",
        "isolated_at":           "2026-10-26T09:41:00+05:30",
        "touched_at":            "2026-10-26T09:21:00+05:30",
        "instrument_ltp":        177.00,
        "instrument_quote_source": "live_feed_cache",
        "instrument_touch_ohlc": {...},
        "instrument_first_candle": {...},
        "instrument_or_levels":  {...},
        "levels":                ["S2", "S3", "S3_Threshold"],
        "promoted_at":           "2026-10-26T09:41:00+05:30",
        "promoted_from_key":     "NSE_FO|51327",
        "promoted_from_symbol":  "NIFTY 22400 PE 27 OCT 26"
      }
    }

Public API
----------
    isolated_instrument_promotion_service.read_active() -> dict | None
    isolated_instrument_promotion_service.apply_promotion(signal) -> dict
    isolated_instrument_promotion_service.clear() -> dict
    isolated_instrument_promotion_service.is_promoted_today() -> bool
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")

_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"

# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def _today_iso() -> str:
    return datetime.now(_MARKET_TZ).date().isoformat()


def _now_market_iso() -> str:
    return datetime.now(_MARKET_TZ).isoformat()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sidecar_path() -> Path:
    return Path(config.LIVE_DATA_DIR) / "level_isolated_instrument_promotion.json"


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_MARKET_TZ)
    return dt


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_first_candle_json(
    underlying: str,
    strike: Any,
    side: str,
) -> dict[str, Any]:
    """Read the option's ``first_candle.json`` from disk (best-effort)."""
    try:
        strike_int = int(float(strike))
    except (TypeError, ValueError):
        return {}
    path = (
        Path(config.LIVE_DATA_DIR)
        / str(underlying).strip().lower()
        / f"{strike_int}_{str(side).strip().upper()}"
        / "first_candle.json"
    )
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _confirmation_bar_ohlc(
    instrument_key: str,
    confirmed_time_iso: str,
) -> dict[str, Any]:
    """Return OHLC of the 1-min candle that ended at ``confirmed_time_iso``."""
    try:
        from services.live_sr_levels_service import live_sr_levels_service
    except Exception:
        return {}

    try:
        with live_sr_levels_service._guard:  # type: ignore[attr-defined]
            state = live_sr_levels_service._states.get(instrument_key)  # type: ignore[attr-defined]
    except Exception:
        return {}

    if not state:
        return {}

    target = _parse_iso_datetime(confirmed_time_iso)
    if target is None:
        return {}
    target_minute = target.replace(second=0, microsecond=0)

    for c in state.get("candles") or []:
        c_dt = _parse_iso_datetime(c.get("time"))
        if c_dt is None:
            continue
        if c_dt.replace(second=0, microsecond=0) == target_minute:
            return {
                "time_iso": c.get("time"),
                "open": _safe_float(c.get("open")),
                "high": _safe_float(c.get("high")),
                "low": _safe_float(c.get("low")),
                "close": _safe_float(c.get("close")),
                "volume": c.get("volume"),
            }
    return {}


def _live_ltp(instrument_key: str) -> tuple[float | None, str]:
    try:
        from services.live_feed_service import live_feed_service
        ltp = live_feed_service.get_latest_ltp(instrument_key)
        if ltp is not None:
            return float(ltp), "live_feed_cache"
    except Exception:
        pass
    return None, "unavailable"


# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class IsolatedInstrumentPromotionService:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # State accessors                                                     #
    # ------------------------------------------------------------------ #
    def read_active(self) -> dict[str, Any] | None:
        """Return today's promotion record, or None."""
        path = _sidecar_path()
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[promotion] unreadable sidecar file")
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("day") or "") != _today_iso():
            return None
        return payload

    def is_promoted_today(self) -> bool:
        return self.read_active() is not None

    # ------------------------------------------------------------------ #
    # Apply promotion                                                     #
    # ------------------------------------------------------------------ #
    def apply_promotion(self, signal: dict[str, Any]) -> dict[str, Any]:
        if not config.STRATEGY_ENTRY_ALLOW_PROMOTION:
            return {
                "promoted": False,
                "reason": "promotion_disabled",
                "generated_at": _now_utc_iso(),
            }

        if not isinstance(signal, dict) or signal.get("status") != "ok":
            return {
                "promoted": False,
                "reason": "no_valid_signal",
                "generated_at": _now_utc_iso(),
            }

        promotion = signal.get("promotion") or {}
        if not promotion.get("recommended"):
            return {
                "promoted": False,
                "reason": "not_recommended",
                "generated_at": _now_utc_iso(),
            }

        target_key = str(promotion.get("target_instrument_key") or "").strip()
        if not target_key:
            return {
                "promoted": False,
                "reason": "no_target",
                "generated_at": _now_utc_iso(),
            }

        with self._lock:
            existing = self.read_active()
            if existing is not None:
                existing_key = str(existing.get("to_instrument_key") or "")
                if existing_key == target_key:
                    return {
                        "promoted": False,
                        "reason": "already_promoted",
                        "existing": existing,
                        "generated_at": _now_utc_iso(),
                    }
                if not config.STRATEGY_ENTRY_ALLOW_RE_PROMOTION:
                    return {
                        "promoted": False,
                        "reason": "already_promoted_other_target",
                        "existing": existing,
                        "generated_at": _now_utc_iso(),
                    }

            # Find the target candidate in the signal's list.
            target_cand: dict[str, Any] | None = None
            for c in signal.get("candidates") or []:
                if str(c.get("instrument_key") or "") == target_key:
                    target_cand = c
                    break
            if target_cand is None:
                return {
                    "promoted": False,
                    "reason": "target_not_in_candidates",
                    "generated_at": _now_utc_iso(),
                }

            isolated_block = signal.get("isolated") or {}
            underlying = str(isolated_block.get("underlying") or "").strip()
            strike = isolated_block.get("strike")
            if not underlying or strike is None:
                return {
                    "promoted": False,
                    "reason": "missing_underlying_or_strike",
                    "generated_at": _now_utc_iso(),
                }

            side = str(target_cand.get("side") or "").strip().upper()
            if side not in {"CE", "PE"}:
                return {
                    "promoted": False,
                    "reason": "invalid_side",
                    "generated_at": _now_utc_iso(),
                }

            # Registry entry (for trading_symbol / expiry / levels)
            try:
                from services.instrument_registry import instrument_registry
                reg_entry = instrument_registry.get(target_key) or {}
            except Exception:
                reg_entry = {}

            trading_symbol = (
                target_cand.get("trading_symbol")
                or reg_entry.get("trading_symbol")
                or target_key
            )
            expiry = (
                target_cand.get("expiry")
                or reg_entry.get("expiry")
            )

            # ---- Capture the promoted instrument's snapshot -------- #
            ltp, ltp_source = _live_ltp(target_key)
            first_candle_payload = _read_first_candle_json(
                underlying, strike, side,
            )
            fc_obj = first_candle_payload.get("first_candle") or {}
            or_levels = first_candle_payload.get("or_levels") or {}

            entry_support = target_cand.get("entry_support") or {}
            entry = target_cand.get("entry") or {}
            confirmed_time_iso = str(entry_support.get("confirmed_time") or "")
            touch_ohlc = (
                _confirmation_bar_ohlc(target_key, confirmed_time_iso)
                if confirmed_time_iso else {}
            )

            # LTP fallback chain
            quote_source = ltp_source
            if ltp is None and entry.get("price") is not None:
                ltp = _safe_float(entry.get("price"))
                quote_source = "entry_ltp_fallback"
            if ltp is None and touch_ohlc.get("close") is not None:
                ltp = _safe_float(touch_ohlc.get("close"))
                quote_source = "confirmation_bar_close"

            trigger_info = signal.get("touch") or {}

            record = {
                "instrument_key": target_key,
                "trading_symbol": trading_symbol,
                "underlying": underlying,
                "strike": strike,
                "type": side,
                "expiry": expiry,
                "reason": isolated_block.get("reason"),
                "trigger_level": (
                    isolated_block.get("trigger_level")
                    or trigger_info.get("side")
                ),
                "trigger_level_value": isolated_block.get("trigger_level_value"),
                "trigger_index": (
                    isolated_block.get("trigger_index")
                    or trigger_info.get("bias_type")
                ),
                "isolated_at": _now_market_iso(),
                "touched_at": (
                    trigger_info.get("time")
                    or isolated_block.get("touched_at")
                ),
                "instrument_ltp": ltp,
                "instrument_quote_source": quote_source,
                "instrument_touch_ohlc": touch_ohlc,
                "instrument_first_candle": fc_obj,
                "instrument_or_levels": or_levels,
                "levels": (
                    target_cand.get("levels")
                    or reg_entry.get("levels")
                    or []
                ),
                "promoted_at": _now_market_iso(),
                "promoted_from_key": (
                    (existing or {}).get("to_instrument_key")
                    or isolated_block.get("instrument_key")
                ),
                "promoted_from_symbol": (
                    (existing or {}).get("to_trading_symbol")
                    or isolated_block.get("trading_symbol")
                ),
            }

            payload = {
                "day": _today_iso(),
                "promoted_at": _now_market_iso(),
                "from_instrument_key": record["promoted_from_key"],
                "from_trading_symbol": record["promoted_from_symbol"],
                "to_instrument_key": target_key,
                "to_trading_symbol": trading_symbol,
                "reason": "strategy_entry_armed",
                "evidence": {
                    "entry_support_price": entry_support.get("price"),
                    "entry_support_pivot_time": entry_support.get("pivot_time"),
                    "entry_support_confirmed_time": confirmed_time_iso,
                    "entry_ltp": entry.get("price"),
                    "entry_price_source": entry.get("price_source"),
                    "entry_stop": entry.get("stop"),
                    "entry_target": entry.get("target"),
                    "strategy_chosen_side": signal.get("chosen_side"),
                },
                "record": record,
            }

            # ---- Persist sidecar atomically ------------------------ #
            try:
                path = _sidecar_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(payload, indent=2, default=str),
                    encoding="utf-8",
                )
                tmp.replace(path)
            except Exception as exc:
                logger.exception("[promotion] sidecar write failed")
                return {
                    "promoted": False,
                    "reason": f"write_failed: {type(exc).__name__}",
                    "generated_at": _now_utc_iso(),
                }

            # ---- Switch the SR tracker to the promoted instrument -- #
            try:
                from services.isolated_sr_tracker_service import (
                    isolated_sr_tracker_service,
                )
                isolated_sr_tracker_service.on_isolation(record)
            except Exception:
                logger.exception("[promotion] SR tracker switch failed")

            # ---- Telegram (informational) ------------------------- #
            try:
                from services.telegram_service import telegram_service
                msg = (
                    f"{_DIV}\n"
                    f"🎯 INSTRUMENT PROMOTED\n"
                    f"{_DIV}\n"
                    f"📌 From:        "
                    f"{payload['from_trading_symbol']} "
                    f"({payload['from_instrument_key']})\n"
                    f"📌 To:          "
                    f"{payload['to_trading_symbol']} "
                    f"({payload['to_instrument_key']})\n"
                    f"🎯 Reason:      strategy entry ARMED on "
                    f"{signal.get('chosen_side')} side\n"
                    f"🕒 Triggered:   {trigger_info.get('side')} @ "
                    f"{trigger_info.get('time')}\n"
                    f"🕒 Promoted:    {payload['promoted_at']}\n"
                    f"{_THIN}\n"
                    f"💰 Entry LTP:   {entry.get('price')} "
                    f"({entry.get('price_source')})\n"
                    f"🛑 Stop:        {entry.get('stop')}\n"
                    f"🎯 Target:      {entry.get('target')}\n"
                    f"{_THIN}\n"
                    f"📊 SR tracker is now tracking the promoted instrument.\n"
                    f"💾 Saved to data/level_isolated_instrument_promotion.json"
                )
                telegram_service.send_message(
                    "Instrument Promoted", msg, level="SUCCESS",
                )
            except Exception:
                logger.exception("[promotion] Telegram send failed")

            logger.info(
                "[promotion] PROMOTED %s -> %s (reason=%s, entry_ltp=%s)",
                payload["from_instrument_key"],
                payload["to_instrument_key"],
                payload["reason"],
                entry.get("price"),
            )

            return {
                "promoted": True,
                "promotion": payload,
                "generated_at": _now_utc_iso(),
            }

    # ------------------------------------------------------------------ #
    # Clear                                                               #
    # ------------------------------------------------------------------ #
    def clear(self) -> dict[str, Any]:
        with self._lock:
            path = _sidecar_path()
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                logger.exception("[promotion] sidecar delete failed")
                return {
                    "status": "error",
                    "error": "delete_failed",
                    "generated_at": _now_utc_iso(),
                }
            try:
                from services.isolated_sr_tracker_service import (
                    isolated_sr_tracker_service,
                )
                isolated_sr_tracker_service.reset()
            except Exception:
                logger.exception("[promotion] SR tracker reset failed")
            logger.info("[promotion] state cleared")
            return {"status": "ok", "generated_at": _now_utc_iso()}


isolated_instrument_promotion_service = IsolatedInstrumentPromotionService()