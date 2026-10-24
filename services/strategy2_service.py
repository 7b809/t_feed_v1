# File: services/strategy2_service.py
"""Strategy 2 — first-candle EMA cross did NOT happen (or S1 didn't fire).

Watches R3 up-cross and S3 down-cross on ALL option instruments. When an
R3 fires on an instrument, we assume the paired S3 (same strike, opposite
type) is going to fire (or already fired). Once the pair is confirmed,
emit a Telegram message: "ISOLATION SELECTED".

No order is placed by Strategy 2. It is a Telegram-only isolation alert.

Range filter
------------
Same as Strategy 1:
* NIFTY strike within NIFTY avg +/- STRATEGY2_NIFTY_RANGE_POINTS.
* SENSEX strike within SENSEX avg +/- STRATEGY2_SENSEX_RANGE_POINTS.

Pair rule
---------
For instrument X (say 22700 CE) that hits R3, the paired instrument is
the same-strike opposite type (22700 PE). When the pair's S3 fires (or
already fired), we emit "isolation selected" once per pair per day.

Persistence
-----------
``meta_data/strategy2_isolation_YYYY-MM-DD.json`` — list of emitted
pairs for the day.
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
_ISO_DIR = Path(config.PROJECT_ROOT) / "meta_data"
_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"


def _today_iso() -> str:
    return datetime.now(_TZ).date().isoformat()


def _iso_path(day: str) -> Path:
    _ISO_DIR.mkdir(parents=True, exist_ok=True)
    return _ISO_DIR / f"strategy2_isolation_{day}.json"


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
        band = float(getattr(config, "STRATEGY2_NIFTY_RANGE_POINTS", 300.0))
        return (nifty_avg - band) <= strike <= (nifty_avg + band)
    if _is_sensex(entry):
        if sensex_avg is None:
            return False
        band = float(getattr(config, "STRATEGY2_SENSEX_RANGE_POINTS", 700.0))
        return (sensex_avg - band) <= strike <= (sensex_avg + band)
    return False


class Strategy2Service:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: str | None = None
        # Set of pair_ids already emitted, e.g. "NIFTY:22700"
        self._emitted: set[str] = set()
        # R3 hits waiting for S3 confirmation: pair_id -> metadata
        self._pending_r3: dict[str, dict[str, Any]] = {}
        # S3 hits waiting for R3 confirmation: pair_id -> metadata
        self._pending_s3: dict[str, dict[str, Any]] = {}
        self._records: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # Touch handler                                                      #
    # ------------------------------------------------------------------ #
    def on_level_touch(self, key: str, level: str, ltp: float) -> None:
        if level not in ("R3", "S3"):
            return
        if not strategy_state_service.is_strategy2_window_open():
            return

        entry = instrument_registry.get(key)
        if entry is None:
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
                "[strategy2] touch OUT OF RANGE — %s %s ltp=%s strike=%s",
                key, level, ltp, entry.get("strike_price"),
            )
            return

        pair_id = self._pair_id(entry)
        if pair_id is None:
            return

        with self._lock:
            self._roll_day()
            if pair_id in self._emitted:
                return

            opposite = instrument_registry.find_opposite(entry)
            opposite_key = str(opposite.get("instrument_key")) if opposite else None

            payload = {
                "instrument_key": key,
                "instrument": entry,
                "opposite_key": opposite_key,
                "level": level,
                "ltp": ltp,
                "ts": datetime.now(_TZ).isoformat(),
            }

            if level == "R3":
                self._pending_r3[pair_id] = payload
                # Check if paired S3 was seen earlier
                if pair_id in self._pending_s3:
                    self._emit_pair(
                        pair_id,
                        r3=payload,
                        s3=self._pending_s3.pop(pair_id),
                    )
            else:  # S3
                self._pending_s3[pair_id] = payload
                if pair_id in self._pending_r3:
                    self._emit_pair(
                        pair_id,
                        r3=self._pending_r3.pop(pair_id),
                        s3=payload,
                    )

    def _pair_id(self, entry: dict[str, Any]) -> str | None:
        underlying = entry.get("underlying")
        strike = entry.get("strike_price")
        if underlying is None or strike is None:
            return None
        try:
            strike_i = int(float(strike))
        except (TypeError, ValueError):
            return None
        u = _underlying_of(entry)
        if not u:
            return None
        return f"{u}:{strike_i}"

    # ------------------------------------------------------------------ #
    # Emit isolation alert                                               #
    # ------------------------------------------------------------------ #
    def _emit_pair(
        self,
        pair_id: str,
        r3: dict[str, Any],
        s3: dict[str, Any],
    ) -> None:
        if pair_id in self._emitted:
            return
        self._emitted.add(pair_id)

        r3_entry = r3["instrument"]
        s3_entry = s3["instrument"]

        record = {
            "day": _today_iso(),
            "pair_id": pair_id,
            "r3": {
                "instrument_key": r3["instrument_key"],
                "trading_symbol": r3_entry.get("trading_symbol"),
                "strike": r3_entry.get("strike_price"),
                "type": r3_entry.get("strike_type"),
                "underlying": r3_entry.get("underlying"),
                "expiry": r3_entry.get("expiry"),
                "ts": r3["ts"],
                "ltp": r3["ltp"],
            },
            "s3": {
                "instrument_key": s3["instrument_key"],
                "trading_symbol": s3_entry.get("trading_symbol"),
                "strike": s3_entry.get("strike_price"),
                "type": s3_entry.get("strike_type"),
                "underlying": s3_entry.get("underlying"),
                "expiry": s3_entry.get("expiry"),
                "ts": s3["ts"],
                "ltp": s3["ltp"],
            },
            "emitted_at": datetime.now(_TZ).isoformat(),
        }
        self._records.append(record)
        try:
            self._persist()
        except Exception:
            logger.exception("[strategy2] persist failed")
        self._notify(record)

    def _persist(self) -> None:
        path = _iso_path(_today_iso())
        payload = {
            "day": _today_iso(),
            "updated_at": datetime.now(_TZ).isoformat(),
            "records": self._records,
        }
        path.write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )

    def _notify(self, record: dict[str, Any]) -> None:
        r3 = record["r3"]
        s3 = record["s3"]
        msg = (
            f"{_DIV}\n"
            f"🔒 STRATEGY 2 · ISOLATION SELECTED\n"
            f"{_DIV}\n"
            f"🕒 Emitted:       {record.get('emitted_at')}\n"
            f"🎯 Pair:          {record.get('pair_id')}\n"
            f"{_THIN}\n"
            f"R3 hit:\n"
            f"  Symbol:        {r3.get('trading_symbol')}\n"
            f"  Key:           {r3.get('instrument_key')}\n"
            f"  Strike:        {r3.get('strike')}\n"
            f"  Type:          {r3.get('type')}\n"
            f"  Underlying:    {r3.get('underlying')}\n"
            f"  Expiry:        {r3.get('expiry')}\n"
            f"  Time:          {r3.get('ts')}\n"
            f"  LTP:           {r3.get('ltp')}\n"
            f"{_THIN}\n"
            f"S3 hit (paired):\n"
            f"  Symbol:        {s3.get('trading_symbol')}\n"
            f"  Key:           {s3.get('instrument_key')}\n"
            f"  Strike:        {s3.get('strike')}\n"
            f"  Type:          {s3.get('type')}\n"
            f"  Time:          {s3.get('ts')}\n"
            f"  LTP:           {s3.get('ltp')}\n"
            f"{_DIV}\n"
            f"✅ Selected for isolation"
        )
        try:
            telegram_service.send_message(
                "Strategy 2 · Isolation Selected",
                msg,
                level="INFO",
            )
        except Exception:
            logger.exception("[strategy2] telegram notify failed")

    # ------------------------------------------------------------------ #
    # Rescan on activation (called after 11:00 cutoff)                   #
    # ------------------------------------------------------------------ #
    def rescan_existing_touches(self) -> None:
        """On activation at 11:00, pair up any existing touches."""
        try:
            from services.level_touch_tracker_service import (
                level_touch_tracker_service,
            )
            all_touches = level_touch_tracker_service.get_all_touches()
        except Exception:
            logger.exception("[strategy2] rescan failed reading touches")
            return

        for key, touches in all_touches.items():
            for level_name in ("R3", "S3"):
                hit = touches.get(level_name)
                if not hit:
                    continue
                try:
                    self.on_level_touch(
                        key, level_name, float(hit.get("ltp") or 0.0),
                    )
                except Exception:
                    logger.exception(
                        "[strategy2] rescan replay failed for %s %s",
                        key, level_name,
                    )

    # ------------------------------------------------------------------ #
    # Day rollover                                                       #
    # ------------------------------------------------------------------ #
    def _roll_day(self) -> None:
        today = _today_iso()
        if self._day != today:
            self._day = today
            self._emitted.clear()
            self._pending_r3.clear()
            self._pending_s3.clear()
            self._records = []


strategy2_service = Strategy2Service()