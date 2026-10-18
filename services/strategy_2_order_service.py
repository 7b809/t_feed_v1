"""STRATEGY 2 — Stage 4: next-cross order placement.

Inputs:
    * The isolated contract chosen by Stage 3
      (data/strategy2/stage_3_isolation_level/stage_3_summary.json)
    * That contract's live EMA engine candles (via live_ema_engine)

Logic:
    1. Read the isolated contract and its S3-touch minute.
    2. Scan that contract's EMA candles for the FIRST fast/slow cross
       whose timestamp is STRICTLY AFTER the S3-touch minute.
    3. On that cross:
         bullish  -> BUY  the same contract  (e.g. CE stays CE)
         bearish  -> BUY  the OPPOSITE type  (e.g. CE -> PE @ same strike)
       Use the cross price for the same-type order, or the sibling
       contract's own LTP at that minute for the opposite-type order
       (best-effort; falls back to the cross price when unavailable).

Paper order only — the real Upstox order API is not wired.
The audit payload records: touch minute, cross minute, direction,
chosen instrument, entry price, and the reason.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from services.instrument_registry import instrument_registry
from services.live_ema_engine import _ema_series  # reuse EMA math

logger = logging.getLogger(__name__)

_MARKET_CLOSE_TIME = dtime(15, 30)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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
    return _market_now().time() > _MARKET_CLOSE_TIME

def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)

def _safe(name: Any) -> str:
    text = str(name or "unknown").strip()
    for ch in ("|", "/", "\\", ":", "*", "?", '"', "<", ">", " "):
        text = text.replace(ch, "_")
    return text.strip("._") or "unknown"

def _stage3_root() -> Path:
    return Path(config.STRATEGY_2_BASE_DIR) / config.STRATEGY_2_STAGE_3_NAME

def _stage4_root() -> Path:
    return Path(config.STRATEGY_2_BASE_DIR) / config.STRATEGY_2_STAGE_4_NAME

def _flip_type(strike_type: str) -> str:
    st = str(strike_type or "").upper()
    if st == "CE":
        return "PE"
    if st == "PE":
        return "CE"
    return st

# ---------------------------------------------------------------------------
# Cross detection on a candle series
# ---------------------------------------------------------------------------
def _find_first_cross_after(
    candles: list[dict[str, Any]],
    *,
    since_minute_prefix: str,
) -> dict[str, Any] | None:
    """Return the first fast/slow cross whose minute > since_minute_prefix."""
    if len(candles) < config.EMA_SLOW_PERIOD + 2:
        return None
    closes = [float(c["close"]) for c in candles if c.get("close") is not None]
    valid = [c for c in candles if c.get("close") is not None]
    if len(closes) < config.EMA_SLOW_PERIOD + 2:
        return None

    fast = _ema_series(closes, config.EMA_FAST_PERIOD)
    slow = _ema_series(closes, config.EMA_SLOW_PERIOD)

    for i in range(1, len(closes)):
        minute = str(valid[i].get("minute") or "")
        if minute <= since_minute_prefix:
            continue
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
        return {
            "minute": minute,
            "timestamp": valid[i].get("timestamp"),
            "type": kind,
            "fast_ema": round(f1, 4),
            "slow_ema": round(s1, 4),
            "ema_diff": round(now_diff, 4),
            "close": closes[i],
        }
    return None

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class Strategy2OrderService:

    def run(
        self,
        reason: str = "manual",
        *,
        mode: str | None = None,
        catchup: bool = False,
    ) -> dict[str, Any]:
        effective_mode = str(mode or reason or "manual")
        post_market = _post_market()

        logger.info(
            "[strategy2:%s] stage_4 run (mode=%s catchup=%s post_market=%s)",
            reason, effective_mode, catchup, post_market,
        )

        if not config.STRATEGY_2_ENABLED or not config.STRATEGY_2_ORDER_ENABLED:
            return self._summary_disabled(reason, effective_mode, catchup, post_market)

        summary_path = _stage3_root() / "stage_3_summary.json"
        stage3 = _read_json(summary_path) or {}
        isolated = stage3.get("isolated") or None

        if not isolated:
            logger.info("[strategy2:%s] stage_4: no isolated contract from stage 3", reason)
            return self._summary_no_isolated(reason, effective_mode, catchup, post_market)

        instrument_key = str(isolated.get("instrument_key") or "").strip()
        touch_minute = str(isolated.get("touch_minute") or "").strip()
        if not instrument_key or not touch_minute:
            return self._summary_no_isolated(reason, effective_mode, catchup, post_market)

        # Load candles from the live EMA engine.
        from services.live_ema_engine import live_ema_engine
        payload = live_ema_engine.get_candles(instrument_key, limit=0)
        candles = payload.get("candles") or []

        cross = _find_first_cross_after(candles, since_minute_prefix=touch_minute)
        if cross is None:
            logger.info(
                "[strategy2:%s] stage_4: no cross after %s for %s",
                reason, touch_minute, instrument_key,
            )
            return self._summary_no_cross(
                reason, effective_mode, catchup, post_market,
                isolated=isolated,
            )

        # ---- Decide order direction + instrument -----------------------
        cross_type = str(cross.get("type") or "").lower()
        original_type = str(isolated.get("strike_type") or "").upper()
        entry_ltp = float(cross.get("close") or 0.0)
        entry_ltp_source = "cross_close"
        entry_ltp_minute = cross.get("minute")

        if cross_type == "bullish":
            order_type = original_type
            order_instrument_key = instrument_key
            direction_reason = (
                f"Bullish cross on {original_type} -> BUY same type"
            )
        else:  # bearish
            order_type = _flip_type(original_type)
            order_instrument_key = self._resolve_opposite_key(isolated, order_type)
            # Try to fetch the sibling's own LTP at the same minute.
            sibling_ltp = self._sibling_ltp_at(
                order_instrument_key, str(cross.get("minute") or "")
            )
            if sibling_ltp is not None:
                entry_ltp = float(sibling_ltp)
                entry_ltp_source = "sibling_ltp_at_cross_minute"
            direction_reason = (
                f"Bearish cross on {original_type} -> BUY opposite {order_type}"
            )

        order = {
            "day": isolated.get("day"),
            "strategy": "strategy_2",
            "ticker": isolated.get("ticker"),
            "index_name": isolated.get("index_name"),
            "index_key": isolated.get("index_key"),
            "isolation_level_name": isolated.get("isolation_level_name"),
            "isolation_level": isolated.get("isolation_level"),
            "s3_touch_minute": touch_minute,
            "s3_touch_ltp": isolated.get("touch_ltp"),
            "cross_minute": cross.get("minute"),
            "cross_type": cross_type,
            "original_strike_type": original_type,
            "order_strike_type": order_type,
            "order_instrument_key": order_instrument_key,
            "strike_price": isolated.get("strike_price"),
            "side": "BUY",
            "quantity": config.STRATEGY_2_ORDER_QTY,
            "entry_ltp": entry_ltp,
            "entry_ltp_source": entry_ltp_source,
            "entry_ltp_minute": entry_ltp_minute,
            "status": "paper_filled",
            "execution_mode": effective_mode,
            "catchup": bool(catchup),
            "simulation": bool(catchup),
            "post_market": post_market,
            "trigger": str(reason or mode),
            "actual_time_ist": _market_hhmmss(),
            "selected_at": _now_iso(),
            "placed_at": _now_iso(),
            "explanation": (
                f"Stage 3 isolated {isolated.get('ticker')} "
                f"{isolated.get('strike_price')} {original_type} after its "
                f"own {isolated.get('isolation_level_name')} "
                f"({isolated.get('isolation_level')}) was touched at "
                f"{touch_minute} (ltp {isolated.get('touch_ltp')}). "
                f"Stage 4 then waited for the next EMA cross on the same "
                f"contract. The first cross after the touch was "
                f"{cross_type} at {cross.get('minute')} "
                f"(close {entry_ltp}). {direction_reason}. "
                f"Entry LTP source = {entry_ltp_source}."
            ),
            "note": (
                "Paper order — real Upstox order API is not wired. "
                "Wire /v2/order/place here to go live."
            ),
        }

        # Persist per-contract folder + top-level summary.
        base = _stage4_root()
        base.mkdir(parents=True, exist_ok=True)
        out_dir = (
            base
            / _safe(isolated.get("ticker"))
            / _safe(isolated.get("index_name"))
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        orders_path = out_dir / "orders.json"
        existing = _read_json(orders_path) or {
            "day": order["day"],
            "ticker": order["ticker"],
            "index_name": order["index_name"],
            "orders": [],
        }
        existing["orders"].append(order)
        existing["updated_at"] = _now_iso()
        _write_json(orders_path, existing)

        meta_path = out_dir / "order_meta_test.json"
        _write_json(meta_path, {
            "strategy": "strategy_2",
            "stage": "stage_4",
            "mode": effective_mode,
            "catchup": bool(catchup),
            "post_market": post_market,
            "day": order["day"],
            "order": order,
            "audit": {
                "isolation_level": isolated.get("isolation_level_name"),
                "s3_touch_minute": touch_minute,
                "cross_minute": cross.get("minute"),
                "cross_type": cross_type,
                "direction_rule": direction_reason,
            },
        })

        summary = {
            "status": "ok",
            "stage": "stage_4",
            "strategy": "strategy_2",
            "reason": reason,
            "mode": effective_mode,
            "catchup": bool(catchup),
            "post_market": post_market,
            "generated_at": _now_iso(),
            "day": order["day"],
            "isolated": isolated,
            "cross": cross,
            "placed": 1,
            "order": order,
        }
        _write_json(base / "stage_4_summary.json", summary)

        logger.info(
            "[strategy2:%s] stage_4 COMPLETE — placed=1 ticker=%s strike=%s "
            "type=%s cross=%s @ %s",
            reason,
            order["ticker"],
            order["strike_price"],
            order["order_strike_type"],
            cross_type,
            cross.get("minute"),
        )
        return summary

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    def _resolve_opposite_key(
        self, isolated: dict[str, Any], opposite_type: str
    ) -> str | None:
        """Lookup the sibling (opposite-type) contract from the registry."""
        ticker = isolated.get("ticker")
        strike = isolated.get("strike_price")
        if not ticker or strike is None:
            return None
        try:
            entry = instrument_registry.resolve(
                str(ticker), strike, opposite_type
            )
            if entry:
                return entry["instrument_key"]
        except Exception:
            logger.exception(
                "[strategy2] opposite-key resolve failed for %s %s %s",
                ticker, strike, opposite_type,
            )
        return None

    def _sibling_ltp_at(
        self, instrument_key: str | None, minute: str
    ) -> float | None:
        """Return the sibling contract's close at the given minute (best effort)."""
        if not instrument_key or not minute:
            return None
        try:
            from services.live_ema_engine import live_ema_engine
            payload = live_ema_engine.get_candles(instrument_key, limit=0)
            for c in payload.get("candles") or []:
                if str(c.get("minute")) == minute:
                    try:
                        return float(c.get("close"))
                    except (TypeError, ValueError):
                        return None
        except Exception:
            logger.exception(
                "[strategy2] sibling LTP fetch failed for %s",
                instrument_key,
            )
        return None

    def _summary_disabled(self, reason, mode, catchup, post_market) -> dict[str, Any]:
        return {
            "status": "disabled",
            "stage": "stage_4",
            "strategy": "strategy_2",
            "reason": reason, "mode": mode,
            "catchup": bool(catchup), "post_market": post_market,
            "generated_at": _now_iso(),
            "placed": 0, "order": None,
        }

    def _summary_no_isolated(self, reason, mode, catchup, post_market) -> dict[str, Any]:
        return {
            "status": "no_isolated",
            "stage": "stage_4",
            "strategy": "strategy_2",
            "reason": reason, "mode": mode,
            "catchup": bool(catchup), "post_market": post_market,
            "generated_at": _now_iso(),
            "placed": 0, "order": None,
        }

    def _summary_no_cross(
        self, reason, mode, catchup, post_market, *, isolated
    ) -> dict[str, Any]:
        return {
            "status": "no_cross_after_touch",
            "stage": "stage_4",
            "strategy": "strategy_2",
            "reason": reason, "mode": mode,
            "catchup": bool(catchup), "post_market": post_market,
            "generated_at": _now_iso(),
            "isolated": isolated,
            "placed": 0, "order": None,
        }

strategy_2_order_service = Strategy2OrderService()