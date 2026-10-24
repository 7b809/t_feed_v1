# File: services/strategy1_service.py
"""Strategy 1 — first-candle EMA cross happened.

Watches R2 up-cross and S2 down-cross on all cross-detected instruments
from 09:16 until 11:00 IST. Fires exactly ONE order per day.

Selection rule (first-wins with priority hints)
-----------------------------------------------
1. If a NIFTY instrument hits R2 or S2 (in range)  -> fire on NIFTY.
2. Else if a SENSEX instrument hits R2 or S2 (in range) AND no NIFTY
   has fired yet                                    -> SENSEX->NIFTY fallback.
3. Once any order is placed, subsequent touches are ignored for the day.

Buy side
--------
We always BUY the up-mover option:
* R2 hit on an instrument  -> buy THAT instrument (it is moving up).
* S2 hit on an instrument  -> buy the OPPOSITE instrument (same strike).
                              e.g. CE hits S2 -> buy PE, PE hits S2 -> buy CE.

Range filter
------------
* NIFTY option strike must be within NIFTY avg +/- STRATEGY1_NIFTY_RANGE_POINTS.
* SENSEX option strike must be within SENSEX avg +/- STRATEGY1_SENSEX_RANGE_POINTS.

SENSEX->NIFTY fallback
----------------------
When only SENSEX fires, we place a NIFTY order on the NIFTY strike
nearest to NIFTY avg, using the same "up-mover type" logic.
* SENSEX CE hit R2 -> buy NIFTY CE nearest avg.
* SENSEX CE hit S2 -> buy NIFTY PE nearest avg.
* SENSEX PE hit R2 -> buy NIFTY PE nearest avg.
* SENSEX PE hit S2 -> buy NIFTY CE nearest avg.

Order placement
---------------
Uses ``upstox_services.place_order`` (OrderApiV3, MARKET, product D).
Respects ``order_runtime_service`` (no order if disabled).
Never raises; logs + Telegram alerts on failure.

Persistence
-----------
``meta_data/strategy1_selection_YYYY-MM-DD.json`` captures the final
decision (or reason for no decision) for audit.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.strategy_state_service import strategy_state_service
from services.telegram_service import telegram_service

logger = get_logger(__file__)

_TZ = ZoneInfo("Asia/Kolkata")
_SEL_DIR = Path(config.PROJECT_ROOT) / "meta_data"
_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"


def _today_iso() -> str:
    return datetime.now(_TZ).date().isoformat()


def _sel_path(day: str) -> Path:
    _SEL_DIR.mkdir(parents=True, exist_ok=True)
    return _SEL_DIR / f"strategy1_selection_{day}.json"


def _underlying_of(entry: dict[str, Any]) -> str:
    return str(entry.get("underlying") or "").upper().replace(" ", "")


def _is_nifty(entry: dict[str, Any]) -> bool:
    u = _underlying_of(entry)
    return u.startswith("NIFTY") and "BANK" not in u and "FIN" not in u and "MIDCP" not in u


def _is_sensex(entry: dict[str, Any]) -> bool:
    return _underlying_of(entry).startswith("SENSEX")


def _in_range(entry: dict[str, Any], nifty_avg: float | None, sensex_avg: float | None) -> bool:
    try:
        strike = float(entry.get("strike_price"))
    except (TypeError, ValueError):
        return False

    if _is_nifty(entry):
        if nifty_avg is None:
            return False
        band = float(getattr(config, "STRATEGY1_NIFTY_RANGE_POINTS", 300.0))
        return (nifty_avg - band) <= strike <= (nifty_avg + band)

    if _is_sensex(entry):
        if sensex_avg is None:
            return False
        band = float(getattr(config, "STRATEGY1_SENSEX_RANGE_POINTS", 700.0))
        return (sensex_avg - band) <= strike <= (sensex_avg + band)

    return False


class Strategy1Service:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: str | None = None
        self._fired_today: bool = False
        self._selection: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # Touch handler                                                      #
    # ------------------------------------------------------------------ #
    def on_level_touch(self, key: str, level: str, ltp: float) -> None:
        """Called by level_touch_tracker for every new level touch."""
        if level not in ("R2", "S2"):
            return
        if not strategy_state_service.is_strategy1_window_open():
            return
        if strategy_state_service.is_strategy1_fired():
            return
        with self._lock:
            self._roll_day()
            if self._fired_today:
                return

        entry = instrument_registry.get(key)
        if entry is None:
            logger.info("[strategy1] touch on unknown instrument %s", key)
            return
        if entry.get("is_index"):
            return
        strike_type = str(entry.get("strike_type") or "").upper()
        if strike_type not in ("CE", "PE"):
            return

        nifty_avg = strategy_state_service.nifty_avg()
        sensex_avg = strategy_state_service.sensex_avg()
        if not _in_range(entry, nifty_avg, sensex_avg):
            logger.info(
                "[strategy1] touch OUT OF RANGE — %s %s ltp=%s strike=%s "
                "(nifty_avg=%s sensex_avg=%s)",
                key, level, ltp, entry.get("strike_price"),
                nifty_avg, sensex_avg,
            )
            return

        if _is_nifty(entry):
            self._fire_primary(entry, level, ltp)
        elif _is_sensex(entry):
            self._fire_sensex_fallback(entry, level, ltp)
        else:
            logger.info(
                "[strategy1] touch on non-nifty/sensex instrument %s", key
            )

    # ------------------------------------------------------------------ #
    # Primary (NIFTY) fire                                               #
    # ------------------------------------------------------------------ #
    def _fire_primary(self, entry: dict[str, Any], level: str, ltp: float) -> None:
        strike_type = str(entry.get("strike_type") or "").upper()
        # R2 -> buy same instrument (up-mover).
        # S2 -> buy opposite instrument (same strike, opposite type).
        if level == "R2":
            target = entry
            reason = f"NIFTY {strike_type} hit R2 (up-mover)"
        else:  # S2
            target = instrument_registry.find_opposite(entry)
            if target is None:
                logger.warning(
                    "[strategy1] opposite instrument missing for %s",
                    entry.get("instrument_key"),
                )
                return
            reason = (
                f"NIFTY {strike_type} hit S2 -> buy opposite "
                f"{target.get('strike_type')}"
            )

        self._execute(
            source="nifty_primary",
            reference_key=str(entry.get("instrument_key")),
            reference_level=level,
            reference_ltp=ltp,
            target=target,
            reason=reason,
        )

    # ------------------------------------------------------------------ #
    # SENSEX -> NIFTY fallback                                           #
    # ------------------------------------------------------------------ #
    def _fire_sensex_fallback(
        self,
        entry: dict[str, Any],
        level: str,
        ltp: float,
    ) -> None:
        nifty_avg = strategy_state_service.nifty_avg()
        if nifty_avg is None:
            logger.warning(
                "[strategy1] SENSEX fallback skipped — nifty_avg unknown"
            )
            return

        strike_type = str(entry.get("strike_type") or "").upper()
        # R2 -> up-mover = same type
        # S2 -> up-mover = opposite type
        target_type = strike_type if level == "R2" else ("PE" if strike_type == "CE" else "CE")

        target = instrument_registry.find_nearest_strike(
            underlying="NIFTY",
            strike_type=target_type,
            target_price=nifty_avg,
        )
        if target is None:
            logger.warning(
                "[strategy1] SENSEX fallback — no NIFTY %s strike in registry",
                target_type,
            )
            return

        reason = (
            f"SENSEX {strike_type} hit {level} -> NIFTY {target_type} "
            f"nearest avg ({nifty_avg:.2f})"
        )
        self._execute(
            source="sensex_fallback",
            reference_key=str(entry.get("instrument_key")),
            reference_level=level,
            reference_ltp=ltp,
            target=target,
            reason=reason,
        )

    # ------------------------------------------------------------------ #
    # Order execution                                                    #
    # ------------------------------------------------------------------ #
    def _execute(
        self,
        *,
        source: str,
        reference_key: str,
        reference_level: str,
        reference_ltp: float,
        target: dict[str, Any],
        reason: str,
    ) -> None:
        with self._lock:
            if self._fired_today:
                return
            self._fired_today = True

        target_key = str(target.get("instrument_key") or "")
        target_symbol = str(target.get("trading_symbol") or target_key)
        lots = int(getattr(config, "STRATEGY1_LOTS", 1))
        txn = str(getattr(config, "STRATEGY1_TRANSACTION_TYPE", "BUY")).upper()

        record: dict[str, Any] = {
            "day": _today_iso(),
            "fired_at": datetime.now(_TZ).isoformat(),
            "source": source,
            "reason": reason,
            "reference_instrument_key": reference_key,
            "reference_level": reference_level,
            "reference_ltp": reference_ltp,
            "target_instrument_key": target_key,
            "target_trading_symbol": target_symbol,
            "target_strike": target.get("strike_price"),
            "target_type": target.get("strike_type"),
            "target_underlying": target.get("underlying"),
            "lots": lots,
            "transaction_type": txn,
            "order_status": "pending",
            "order_response": None,
            "order_error": None,
        }

        try:
            from upstox_services.place_order import place_order
            response = place_order(
                target_key,
                lots=lots,
                transaction_type=txn,
                product="D",
                order_type="MARKET",
            )
            record["order_status"] = str(response.get("status") or "unknown")
            record["order_response"] = response
            if record["order_status"] != "ok":
                record["order_error"] = response.get("error")
        except Exception as exc:
            logger.exception("[strategy1] place_order raised")
            record["order_status"] = "exception"
            record["order_error"] = f"{type(exc).__name__}: {exc}"

        self._persist(record)
        self._notify(record)

        if record["order_status"] == "ok":
            try:
                strategy_state_service.on_strategy1_fired(
                    target_key=target_key,
                    source=source,
                    details=record,
                )
            except Exception:
                logger.exception("[strategy1] state update failed")
        else:
            # Order did not succeed — do NOT mark as fired so the user
            # can inspect. Also do not suppress Strategy 2 (since no order).
            with self._lock:
                self._fired_today = False
            try:
                strategy_state_service.on_strategy1_error(
                    str(record.get("order_error") or record["order_status"])
                )
            except Exception:
                pass

    def _persist(self, record: dict[str, Any]) -> None:
        path = _sel_path(record["day"])
        try:
            path.write_text(
                json.dumps(record, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("[strategy1] selection write failed")
        with self._lock:
            self._selection = record

    # ------------------------------------------------------------------ #
    # Telegram notification                                              #
    # ------------------------------------------------------------------ #
    def _notify(self, record: dict[str, Any]) -> None:
        status = record.get("order_status")
        ok = status == "ok"
        emoji = "✅" if ok else "❌"
        title = "STRATEGY 1 · ORDER PLACED" if ok else "STRATEGY 1 · ORDER FAILED"

        msg = (
            f"{_DIV}\n"
            f"{emoji} {title}\n"
            f"{_DIV}\n"
            f"🕒 Time:        {record.get('fired_at')}\n"
            f"📊 Source:      {record.get('source')}\n"
            f"{_THIN}\n"
            f"Reference:\n"
            f"  Key:        {record.get('reference_instrument_key')}\n"
            f"  Level:      {record.get('reference_level')}\n"
            f"  LTP:        {record.get('reference_ltp')}\n"
            f"{_THIN}\n"
            f"Target:\n"
            f"  Symbol:     {record.get('target_trading_symbol')}\n"
            f"  Key:        {record.get('target_instrument_key')}\n"
            f"  Strike:     {record.get('target_strike')}\n"
            f"  Type:       {record.get('target_type')}\n"
            f"  Underlying: {record.get('target_underlying')}\n"
            f"{_THIN}\n"
            f"Order:\n"
            f"  Type:       {record.get('transaction_type')}\n"
            f"  Lots:       {record.get('lots')}\n"
            f"  Status:     {status}\n"
        )
        if record.get("order_error"):
            msg += f"  Error:      {record.get('order_error')}\n"
        msg += f"{_DIV}"

        try:
            telegram_service.send_message(
                title,
                msg,
                level="SUCCESS" if ok else "ERROR",
            )
        except Exception:
            logger.exception("[strategy1] telegram notify failed")

    # ------------------------------------------------------------------ #
    # Day rollover                                                       #
    # ------------------------------------------------------------------ #
    def _roll_day(self) -> None:
        today = _today_iso()
        if self._day != today:
            self._day = today
            self._fired_today = False
            self._selection = {}


strategy1_service = Strategy1Service()