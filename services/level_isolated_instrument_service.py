# services/level_isolated_instrument_service.py
"""Level-isolation engine — pick one filtered CE/PE instrument when
NIFTY or SENSEX touches R3 / S3.

Design
------
* Trigger: NIFTY or SENSEX index LTP crosses its own R3 (>=) or S3 (<=),
  read from ``data/index/<idx>/first_candle.json -> or_levels``.
* Candidate instruments come from ``data/index/<idx>/level_instruments.json``
  (produced by ``level_instrument_filter_service``). First preference:
  instruments whose ``levels`` array contains ``R3`` / ``R3_Threshold``
  (CE) or ``S3`` / ``S3_Threshold`` (PE). Fallback: all instruments of
  that side.
* Priority (one-shot per side, per day):
      NIFTY-R3 -> NIFTY-S3 -> SENSEX-R3 -> SENSEX-S3
* NIFTY / SENSEX pick: nearest strike to the index LTP that triggered
  the touch.
* SENSEX-only touch: read the NIFTY LTP *at that moment* (cached from the
  last NIFTY tick). If it is fresh (``LEVEL_ISOLATION_NIFTY_TICK_STALE_SECONDS``),
  pick the **nearest NIFTY filtered instrument** (CE or PE) to that LTP.
  If stale, fall back to SENSEX's own nearest candidate.
* Persisted result: ``data/<LEVEL_ISOLATION_FILENAME>`` — one JSON doc
  per day, containing ``isolated``, ``events[]``, ``touch_state``.

Startup backfill
----------------
Between ``LEVEL_ISOLATION_BACKFILL_WINDOW_START_*`` and ``_END_*`` on a
trading day, ``run_backfill()`` replays today's 1-min intraday candles
minute-by-minute and reconstructs the "would-have-isolated" instrument:
  * touch detection uses bar **high** for R3, bar **low** for S3;
  * LTP-based decisions (nearest strike) use bar **close**.

UI-facing detail endpoint
-------------------------
``get_detail()`` returns a JSON-safe snapshot for the ``/isolated`` HTML
page and the ``GET /admin/level-isolation/detail`` JSON endpoint:
  * ``market`` — trading-day / session state + a human-readable message
    that distinguishes weekend / holiday / pre-open / OR warming-up /
    in-session / post-close.
  * ``isolated`` — the persisted isolation record (or ``None``).
  * ``trigger_event`` — the event that produced the isolation (with the
    full candidate list + per-candidate distances).
  * ``candidates`` — convenience copy of ``trigger_event.candidates``.
  * ``events`` — the full day's touch log.
  * ``touch_state`` — per (index, side) one-shot flags.
  * ``or_levels`` — the reference OR levels currently on disk.

Safety
------
* Never raises for expected failure modes; always returns a summary.
* Non-trading hours / non-trading days / missing OR files → no-op.
* Atomic writes (``.tmp`` + ``replace``) so a crash cannot corrupt the
  persisted state file.
* Telegram fires from a **background daemon thread** so the live feed
  loop is never blocked by an HTTP call.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")

# Registry key for the two indexes we care about.
CORE_INDEX_KEYS: dict[str, str] = {
    "NSE_INDEX|Nifty 50": "NIFTY",
    "BSE_INDEX|SENSEX":   "SENSEX",
}

_R3_TRIGGER_LEVELS = ("R3", "R3_Threshold")
_S3_TRIGGER_LEVELS = ("S3", "S3_Threshold")

_MARKET_OPEN = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)
_OR_READY = dtime(9, 16, 10)
_VALID_WEEKDAYS = {0, 1, 2, 3, 4}


def _now_market() -> datetime:
    return datetime.now(_MARKET_TZ)


def _today_iso() -> str:
    return _now_market().date().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LevelIsolatedInstrumentService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._empty_state()
        self._last_nifty_ltp: float | None = None
        self._last_nifty_tick_at: datetime | None = None
        self._last_sensex_ltp: float | None = None
        self._last_sensex_tick_at: datetime | None = None
        self._last_check_monotonic: float = 0.0
        self._load_or_create_state()

    # ------------------------------------------------------------------ #
    # State file I/O                                                     #
    # ------------------------------------------------------------------ #
    def _state_path(self) -> Path:
        return Path(config.LIVE_DATA_DIR) / config.LEVEL_ISOLATION_FILENAME

    def _empty_state(self) -> dict[str, Any]:
        return {
            "day": _today_iso(),
            "isolated": None,
            "events": [],
            "touch_state": {
                "NIFTY_R3": False, "NIFTY_S3": False,
                "SENSEX_R3": False, "SENSEX_S3": False,
            },
            "last_update": None,
        }

    def _load_or_create_state(self) -> None:
        path = self._state_path()
        if not path.exists():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[level-isolation] unreadable state file")
            return
        if not isinstance(loaded, dict):
            return
        if loaded.get("day") != self._state["day"]:
            return
        self._state = loaded

    def _save_state(self) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._state["last_update"] = _now_iso()
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._state, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            logger.exception("[level-isolation] failed saving state")

    def _ensure_today(self) -> None:
        today = _today_iso()
        if self._state.get("day") != today:
            self._state = self._empty_state()
            self._last_nifty_ltp = None
            self._last_nifty_tick_at = None
            self._last_sensex_ltp = None
            self._last_sensex_tick_at = None
            self._last_check_monotonic = 0.0
            self._load_or_create_state()

    # ------------------------------------------------------------------ #
    # Market gates                                                       #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _in_market_hours(now: datetime) -> bool:
        return (
            now.weekday() in _VALID_WEEKDAYS
            and _MARKET_OPEN <= now.time() <= _MARKET_CLOSE
        )

    def _is_trading_day(self, now: datetime) -> bool:
        if now.weekday() not in _VALID_WEEKDAYS:
            return False
        try:
            from services.market_holiday_service import market_holiday_service
            payload = market_holiday_service.get_today()
            return str(payload.get("status") or "").lower() != "holiday"
        except Exception:
            # Provider error -> assume trading day (never silently skip).
            return True

    def _in_backfill_window(self, now: datetime) -> bool:
        start = dtime(
            config.LEVEL_ISOLATION_BACKFILL_WINDOW_START_HOUR,
            config.LEVEL_ISOLATION_BACKFILL_WINDOW_START_MINUTE,
        )
        end = dtime(
            config.LEVEL_ISOLATION_BACKFILL_WINDOW_END_HOUR,
            config.LEVEL_ISOLATION_BACKFILL_WINDOW_END_MINUTE,
        )
        return start <= now.time() <= end

    # ------------------------------------------------------------------ #
    # Live tick entry point                                              #
    # ------------------------------------------------------------------ #
    def on_tick(self, instrument_key: str, ltp: Any) -> None:
        """Called from ``live_feed_service._handle_feed`` for NIFTY/SENSEX."""
        if not config.LEVEL_ISOLATION_ENABLED:
            return

        canonical = CORE_INDEX_KEYS.get(instrument_key)
        if not canonical:
            return

        try:
            ltp_f = float(ltp)
        except (TypeError, ValueError):
            return

        now = _now_market()
        if not self._in_market_hours(now):
            return

        with self._lock:
            self._ensure_today()

            if canonical == "NIFTY":
                self._last_nifty_ltp = ltp_f
                self._last_nifty_tick_at = now
            else:
                self._last_sensex_ltp = ltp_f
                self._last_sensex_tick_at = now

            now_mono = time.monotonic()
            if (
                now_mono - self._last_check_monotonic
                < config.LEVEL_ISOLATION_CHECK_INTERVAL_SECONDS
            ):
                return
            self._last_check_monotonic = now_mono

            self._check_touches(now)

    # ------------------------------------------------------------------ #
    # Input file readers                                                 #
    # ------------------------------------------------------------------ #
    def _load_or_levels(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for index_name, idx_lower in (("NIFTY", "nifty"), ("SENSEX", "sensex")):
            path = Path(config.LIVE_DATA_DIR) / "index" / idx_lower / "first_candle.json"
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("[level-isolation] unreadable %s", path)
                continue
            if str(payload.get("date") or "") != _today_iso():
                continue
            levels = payload.get("or_levels") or {}
            if levels:
                out[index_name] = levels
        return out

    def _load_filtered_instruments(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for index_name, idx_lower in (("NIFTY", "nifty"), ("SENSEX", "sensex")):
            path = (
                Path(config.LIVE_DATA_DIR)
                / "index" / idx_lower / "level_instruments.json"
            )
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("[level-isolation] unreadable %s", path)
                continue
            if str(payload.get("date") or "") != _today_iso():
                continue
            out[index_name] = payload
        return out

    # ------------------------------------------------------------------ #
    # Live touch checks                                                  #
    # ------------------------------------------------------------------ #
    def _check_touches(self, now: datetime) -> None:
        if self._state.get("isolated") is not None:
            return

        or_levels = self._load_or_levels()
        filtered = self._load_filtered_instruments()
        if not or_levels or not filtered:
            return

        checks = [
            ("NIFTY",  "R3", self._last_nifty_ltp),
            ("NIFTY",  "S3", self._last_nifty_ltp),
            ("SENSEX", "R3", self._last_sensex_ltp),
            ("SENSEX", "S3", self._last_sensex_ltp),
        ]

        for index_name, side, ltp in checks:
            if ltp is None:
                continue
            state_key = f"{index_name}_{side}"
            if self._state["touch_state"].get(state_key):
                continue
            level_value = or_levels.get(index_name, {}).get(side)
            if level_value is None:
                continue
            try:
                level_f = float(level_value)
            except (TypeError, ValueError):
                continue

            touched = (
                (side == "R3" and ltp >= level_f)
                or (side == "S3" and ltp <= level_f)
            )
            if not touched:
                continue

            self._state["touch_state"][state_key] = True
            self._process_touch(
                index_name, side, ltp, level_f,
                or_levels, filtered, now,
            )
            if self._state.get("isolated") is not None:
                break

    def _process_touch(
        self,
        index_name: str,
        side: str,
        ltp: float,
        level_value: float,
        or_levels: dict[str, dict[str, Any]],
        filtered: dict[str, dict[str, Any]],
        now: datetime,
    ) -> None:
        candidates = self._find_candidates(index_name, side, filtered)
        event: dict[str, Any] = {
            "index": index_name,
            "side": side,
            "trigger_level": side,
            "trigger_level_value": level_value,
            "trigger_ltp": ltp,
            "touched_at": now.isoformat(),
            "candidates_count": len(candidates),
            "candidates": self._enrich_candidates(candidates, ltp),
        }

        if self._state.get("isolated") is not None:
            event["status"] = "already_isolated"
            event["existing_isolated_instrument"] = (
                self._state["isolated"].get("instrument_key")
            )
            self._state["events"].append(event)
            self._save_state()
            return

        if not candidates:
            event["status"] = "no_candidates"
            self._state["events"].append(event)
            self._save_state()
            logger.warning(
                "[level-isolation] %s %s touched LTP=%s level=%s — no candidates",
                index_name, side, ltp, level_value,
            )
            return

        if index_name == "SENSEX":
            nifty_ltp = self._last_nifty_ltp
            nifty_tick_at = self._last_nifty_tick_at
            if nifty_ltp is not None and nifty_tick_at is not None:
                stale = (now - nifty_tick_at).total_seconds()
                max_stale = config.LEVEL_ISOLATION_NIFTY_TICK_STALE_SECONDS
                if stale <= max_stale:
                    nifty_candidates = self._all_nifty_filtered(filtered)
                    if nifty_candidates:
                        pick = min(
                            nifty_candidates,
                            key=lambda c: abs(self._strike(c) - nifty_ltp),
                        )
                        event["nifty_reference_ltp"] = nifty_ltp
                        event["nifty_reference_at"] = nifty_tick_at.isoformat()
                        event["nifty_reference_candidates_count"] = len(nifty_candidates)
                        event["nifty_reference_candidates"] = self._enrich_candidates(
                            nifty_candidates,
                            nifty_ltp,
                            distance_field="distance_to_nifty_ltp",
                        )
                        self._isolate(
                            pick,
                            f"sensex_{side.lower()}_with_nifty_reference",
                            event,
                        )
                        return
                else:
                    event["nifty_stale_seconds"] = round(stale, 1)
                    logger.warning(
                        "[level-isolation] NIFTY tick stale (%.0fs) at SENSEX "
                        "%s touch — using SENSEX fallback",
                        stale, side,
                    )

            pick = min(candidates, key=lambda c: abs(self._strike(c) - ltp))
            self._isolate(pick, f"sensex_{side.lower()}_fallback", event)
            return

        pick = min(candidates, key=lambda c: abs(self._strike(c) - ltp))
        self._isolate(pick, f"{index_name.lower()}_{side.lower()}_touch", event)

    # ------------------------------------------------------------------ #
    # Candidate helpers                                                  #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _strike(candidate: dict[str, Any]) -> float:
        try:
            return float(candidate.get("strike") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _enrich_candidates(
        self,
        candidates: list[dict[str, Any]],
        ref_ltp: float,
        distance_field: str = "distance_to_trigger_ltp",
    ) -> list[dict[str, Any]]:
        return [
            {
                "instrument_key": c.get("instrument_key"),
                "trading_symbol": c.get("trading_symbol"),
                "strike": c.get("strike"),
                "type": c.get("type"),
                "levels": c.get("levels"),
                distance_field: round(abs(self._strike(c) - ref_ltp), 2),
            }
            for c in candidates[:20]
        ]

    def _find_candidates(
        self,
        index_name: str,
        side: str,
        filtered: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        payload = filtered.get(index_name, {})
        if side == "R3":
            instruments = payload.get("ce_instruments") or []
            trigger_levels = _R3_TRIGGER_LEVELS
        else:
            instruments = payload.get("pe_instruments") or []
            trigger_levels = _S3_TRIGGER_LEVELS

        primary = [
            inst for inst in instruments
            if any(
                lvl in trigger_levels
                for lvl in (inst.get("levels") or [])
            )
        ]
        if primary:
            return primary
        return list(instruments)

    def _all_nifty_filtered(
        self,
        filtered: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        payload = filtered.get("NIFTY", {})
        return (
            list(payload.get("ce_instruments") or [])
            + list(payload.get("pe_instruments") or [])
        )

    # ------------------------------------------------------------------ #
    # Isolation                                                          #
    # ------------------------------------------------------------------ #
    def _isolate(
        self,
        candidate: dict[str, Any],
        reason: str,
        event: dict[str, Any],
    ) -> None:
        isolated = {
            "instrument_key": candidate.get("instrument_key"),
            "trading_symbol": candidate.get("trading_symbol"),
            "strike": candidate.get("strike"),
            "type": candidate.get("type"),
            "expiry": candidate.get("expiry"),
            "levels": candidate.get("levels"),
            "reason": reason,
            "isolated_at": _now_market().isoformat(),
            "trigger_index": event.get("index"),
            "trigger_side": event.get("side"),
            "trigger_level": event.get("trigger_level"),
            "trigger_level_value": event.get("trigger_level_value"),
            "trigger_ltp": event.get("trigger_ltp"),
            "touched_at": event.get("touched_at"),
            "nifty_reference_ltp": event.get("nifty_reference_ltp"),
            "nifty_reference_at": event.get("nifty_reference_at"),
            "nifty_reference_candidates_count": event.get(
                "nifty_reference_candidates_count"
            ),
        }
        self._state["isolated"] = isolated
        event["status"] = "isolated"
        event["isolated_instrument"] = candidate.get("instrument_key")
        self._state["events"].append(event)
        self._save_state()

        logger.info(
            "[level-isolation] ISOLATED key=%s reason=%s trigger=%s %s LTP=%s",
            candidate.get("instrument_key"), reason,
            event.get("index"), event.get("side"), event.get("trigger_ltp"),
        )

        if config.LEVEL_ISOLATION_TELEGRAM_ENABLED:
            threading.Thread(
                target=self._send_telegram,
                args=(isolated,),
                daemon=True,
                name="level-isolation-telegram",
            ).start()

    def _send_telegram(self, isolated: dict[str, Any]) -> None:
        try:
            from services.telegram_service import telegram_service
            _DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            _THIN = "────────────────────────────────"
            msg = (
                f"{_DIV}\n"
                f"🎯 LEVEL ISOLATION · INSTRUMENT LOCKED\n"
                f"{_DIV}\n"
                f"📌 Instrument:    {isolated.get('trading_symbol') or isolated.get('instrument_key')}\n"
                f"🔑 Key:           {isolated.get('instrument_key')}\n"
                f"📊 Strike/Type:   {isolated.get('strike')} {isolated.get('type')}\n"
                f"📅 Expiry:        {isolated.get('expiry')}\n"
                f"🏷️ Levels:        {', '.join(isolated.get('levels') or [])}\n"
                f"🎯 Trigger:       {isolated.get('reason')}\n"
                f"📈 Trigger LTP:   {isolated.get('trigger_ltp')}\n"
                f"📊 Trigger Level: {isolated.get('trigger_level')} = "
                f"{isolated.get('trigger_level_value')}\n"
                f"🕒 Isolated At:   {isolated.get('isolated_at')}\n"
                f"{_THIN}\n"
                f"💾 Saved to data/{config.LEVEL_ISOLATION_FILENAME}"
            )
            telegram_service.send_message(
                "Level Isolation · Instrument Locked",
                msg,
                level="SUCCESS",
            )
        except Exception:
            logger.exception("[level-isolation] telegram send failed")

    # ------------------------------------------------------------------ #
    # Startup backfill                                                   #
    # ------------------------------------------------------------------ #
    def run_backfill(self, reason: str = "startup") -> dict[str, Any]:
        if not config.LEVEL_ISOLATION_ENABLED:
            return {"status": "disabled"}

        now = _now_market()
        if not self._is_trading_day(now):
            return {"status": "skipped", "reason": "non_trading_day"}
        if not self._in_backfill_window(now):
            return {"status": "skipped", "reason": "outside_window"}

        with self._lock:
            self._ensure_today()

            if self._state.get("isolated") is not None:
                return {
                    "status": "already_isolated",
                    "isolated": self._state["isolated"],
                }

            or_levels = self._load_or_levels()
            filtered = self._load_filtered_instruments()
            if not or_levels or not filtered:
                return {"status": "skipped", "reason": "missing_inputs"}

            nifty = self._fetch_intraday("NSE_INDEX|Nifty 50")
            sensex = self._fetch_intraday("BSE_INDEX|SENSEX")
            if not nifty and not sensex:
                return {"status": "skipped", "reason": "no_intraday_data"}

            merged = self._merge_by_minute(nifty, sensex)
            if not merged:
                return {"status": "skipped", "reason": "no_merged_data"}

            logger.info(
                "[level-isolation] backfill replay — reason=%s candles=%d",
                reason, len(merged),
            )

            isolated = self._replay(merged, or_levels, filtered)
            self._save_state()

            return {
                "status": "ok" if isolated else "no_touch",
                "reason": reason,
                "candles_replayed": len(merged),
                "isolated": isolated,
            }

    def _fetch_intraday(self, instrument_key: str) -> list[dict[str, Any]]:
        try:
            import upstox_client
            cfg = upstox_client.Configuration()
            api = upstox_client.HistoryV3Api(upstox_client.ApiClient(cfg))
            response = api.get_intra_day_candle_data(
                instrument_key, "minutes", "1",
            )
            body = response
            if hasattr(response, "to_dict"):
                body = response.to_dict()
            elif hasattr(response, "data"):
                body = response.data

            if not isinstance(body, dict):
                return []
            candles = (body.get("data") or {}).get("candles") or []
            if not isinstance(candles, list):
                return []

            out: list[dict[str, Any]] = []
            for raw in candles:
                if not isinstance(raw, list) or len(raw) < 5:
                    continue
                try:
                    out.append({
                        "time": str(raw[0]),
                        "open": float(raw[1]),
                        "high": float(raw[2]),
                        "low": float(raw[3]),
                        "close": float(raw[4]),
                        "volume": float(raw[5]) if len(raw) > 5 else 0.0,
                    })
                except (ValueError, TypeError):
                    continue
            out.sort(key=lambda c: c["time"])
            return out
        except Exception:
            logger.exception(
                "[level-isolation] intraday fetch failed for %s",
                instrument_key,
            )
            return []

    def _merge_by_minute(
        self,
        nifty: list[dict[str, Any]],
        sensex: list[dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]]:
        n_by = {c["time"][:16]: c for c in nifty if c.get("time")}
        s_by = {c["time"][:16]: c for c in sensex if c.get("time")}
        minutes = sorted(set(n_by.keys()) | set(s_by.keys()))
        return [(m, n_by.get(m), s_by.get(m)) for m in minutes]

    def _replay(
        self,
        merged: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]],
        or_levels: dict[str, dict[str, Any]],
        filtered: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        for minute, n_bar, s_bar in merged:
            if n_bar is not None:
                self._last_nifty_ltp = n_bar["close"]
                self._last_nifty_tick_at = datetime.fromisoformat(minute).replace(
                    tzinfo=_MARKET_TZ,
                )
            if s_bar is not None:
                self._last_sensex_ltp = s_bar["close"]
                self._last_sensex_tick_at = datetime.fromisoformat(minute).replace(
                    tzinfo=_MARKET_TZ,
                )

            checks = [
                ("NIFTY",  "R3", n_bar),
                ("NIFTY",  "S3", n_bar),
                ("SENSEX", "R3", s_bar),
                ("SENSEX", "S3", s_bar),
            ]

            for index_name, side, bar in checks:
                if bar is None:
                    continue
                state_key = f"{index_name}_{side}"
                if self._state["touch_state"].get(state_key):
                    continue
                level_value = or_levels.get(index_name, {}).get(side)
                if level_value is None:
                    continue
                try:
                    level_f = float(level_value)
                except (TypeError, ValueError):
                    continue

                if side == "R3":
                    touched = float(bar["high"]) >= level_f
                else:
                    touched = float(bar["low"]) <= level_f
                if not touched:
                    continue

                self._state["touch_state"][state_key] = True

                decision_ltp = float(bar["close"])
                event: dict[str, Any] = {
                    "index": index_name,
                    "side": side,
                    "trigger_level": side,
                    "trigger_level_value": level_f,
                    "trigger_ltp": decision_ltp,
                    "touched_at": minute + ":00",
                    "source": "backfill",
                    "candidates_count": 0,
                    "candidates": [],
                }

                candidates = self._find_candidates(index_name, side, filtered)
                event["candidates_count"] = len(candidates)
                event["candidates"] = self._enrich_candidates(
                    candidates, decision_ltp,
                )
                if not candidates:
                    event["status"] = "no_candidates"
                    self._state["events"].append(event)
                    continue

                if index_name == "SENSEX":
                    nifty_ref = self._last_nifty_ltp
                    if nifty_ref is not None:
                        nifty_candidates = self._all_nifty_filtered(filtered)
                        if nifty_candidates:
                            pick = min(
                                nifty_candidates,
                                key=lambda c: abs(self._strike(c) - nifty_ref),
                            )
                            event["nifty_reference_ltp"] = nifty_ref
                            event["nifty_reference_candidates_count"] = len(
                                nifty_candidates
                            )
                            event["nifty_reference_candidates"] = (
                                self._enrich_candidates(
                                    nifty_candidates,
                                    nifty_ref,
                                    distance_field="distance_to_nifty_ltp",
                                )
                            )
                            self._isolate(
                                pick,
                                f"sensex_{side.lower()}_with_nifty_reference",
                                event,
                            )
                            return self._state["isolated"]

                    pick = min(
                        candidates,
                        key=lambda c: abs(self._strike(c) - decision_ltp),
                    )
                    self._isolate(pick, f"sensex_{side.lower()}_fallback", event)
                    return self._state["isolated"]

                pick = min(
                    candidates,
                    key=lambda c: abs(self._strike(c) - decision_ltp),
                )
                self._isolate(pick, f"{index_name.lower()}_{side.lower()}_touch", event)
                return self._state["isolated"]

        return None

    # ------------------------------------------------------------------ #
    # Status / detail / reset                                            #
    # ------------------------------------------------------------------ #
    def last_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": config.LEVEL_ISOLATION_ENABLED,
                "day": self._state.get("day"),
                "isolated": self._state.get("isolated"),
                "touch_state": self._state.get("touch_state"),
                "events_count": len(self._state.get("events") or []),
                "events": self._state.get("events") or [],
                "last_update": self._state.get("last_update"),
                "nifty_ltp": self._last_nifty_ltp,
                "sensex_ltp": self._last_sensex_ltp,
                "nifty_tick_at": (
                    self._last_nifty_tick_at.isoformat()
                    if self._last_nifty_tick_at else None
                ),
                "sensex_tick_at": (
                    self._last_sensex_tick_at.isoformat()
                    if self._last_sensex_tick_at else None
                ),
            }

    def _holiday_info(self, now: datetime) -> dict[str, Any]:
        """Return a best-effort holiday classification for ``now``."""
        if now.weekday() not in _VALID_WEEKDAYS:
            return {
                "is_weekend": True,
                "is_holiday": False,
                "description": None,
                "error": None,
            }
        try:
            from services.market_holiday_service import market_holiday_service
            payload = market_holiday_service.get_today()
            status = str(payload.get("status") or "").lower()
            is_holiday = status == "holiday"
            holidays = payload.get("holidays") or []
            desc = None
            if holidays and isinstance(holidays[0], dict):
                desc = holidays[0].get("description")
            return {
                "is_weekend": False,
                "is_holiday": is_holiday,
                "description": desc,
                "error": payload.get("error") if status == "error" else None,
            }
        except Exception as exc:
            return {
                "is_weekend": False,
                "is_holiday": False,
                "description": None,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _market_context(self, now: datetime) -> dict[str, Any]:
        """Classify the current market moment for the UI.

        The returned ``state`` is one of:
          * ``weekend``         — Saturday / Sunday
          * ``holiday``         — trading holiday per Upstox
          * ``pre_open``        — weekday before 09:15 IST
          * ``or_warming_up``   — 09:15 – 09:16:10 IST
          * ``in_session``      — 09:16:10 – 15:30 IST
          * ``post_close``      — after 15:30 IST

        A ``message`` field carries the human-readable copy the UI shows
        in the banner.
        """
        info = self._holiday_info(now)
        is_weekend = info["is_weekend"]
        is_holiday = info["is_holiday"]
        is_trading = not is_weekend and not is_holiday

        or_available_at = now.replace(
            hour=_OR_READY.hour,
            minute=_OR_READY.minute,
            second=_OR_READY.second,
            microsecond=0,
        )
        or_ready = is_trading and now >= or_available_at
        in_session = (
            is_trading
            and _MARKET_OPEN <= now.time() <= _MARKET_CLOSE
        )

        if is_weekend:
            state = "weekend"
            message = (
                f"Weekend ({now.strftime('%A')}) — market closed. "
                f"Isolation resumes on the next weekday."
            )
        elif is_holiday:
            state = "holiday"
            desc = info["description"] or "trading holiday"
            message = f"Market holiday ({desc}). No isolation today."
        elif now.time() < _MARKET_OPEN:
            state = "pre_open"
            message = (
                "Market opens at 09:15 IST. OR levels will be computed at "
                "09:16:10 IST; isolation will begin watching for R3 / S3 "
                "touches right after."
            )
        elif not or_ready:
            state = "or_warming_up"
            message = (
                "First candle closed at 09:16 — OR levels are being computed. "
                "Isolation will begin watching for R3 / S3 touches in a moment."
            )
        elif not in_session:
            state = "post_close"
            message = (
                "Market closed at 15:30 IST. No new isolation until the "
                "next session."
            )
        else:
            state = "in_session"
            message = "Watching NIFTY / SENSEX for R3 / S3 touches."

        return {
            "is_trading_day": is_trading,
            "is_weekend": is_weekend,
            "is_holiday": is_holiday,
            "holiday_description": info["description"],
            "holiday_error": info.get("error"),
            "in_session": in_session,
            "or_ready": or_ready,
            "now_ist": now.isoformat(),
            "or_available_at": or_available_at.isoformat(),
            "session_open": _MARKET_OPEN.isoformat(),
            "session_close": _MARKET_CLOSE.isoformat(),
            "state": state,
            "message": message,
        }

    def get_detail(self) -> dict[str, Any]:
        """Return a UI-facing snapshot of the level-isolation state."""
        with self._lock:
            now = _now_market()
            market = self._market_context(now)
            or_levels = self._load_or_levels()
            isolated = self._state.get("isolated")
            events = list(self._state.get("events") or [])

            trigger_event: dict[str, Any] | None = None
            if isolated:
                for ev in reversed(events):
                    if ev.get("status") == "isolated":
                        trigger_event = ev
                        break

            return {
                "enabled": config.LEVEL_ISOLATION_ENABLED,
                "day": self._state.get("day") or _today_iso(),
                "market": market,
                "isolated": isolated,
                "trigger_event": trigger_event,
                "candidates": (
                    list(trigger_event.get("candidates") or [])
                    if trigger_event else []
                ),
                "events": events,
                "touch_state": self._state.get("touch_state") or {},
                "or_levels": or_levels,
                "last_update": self._state.get("last_update"),
                "generated_at": _now_iso(),
            }

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._state = self._empty_state()
            self._last_nifty_ltp = None
            self._last_nifty_tick_at = None
            self._last_sensex_ltp = None
            self._last_sensex_tick_at = None
            self._last_check_monotonic = 0.0
            self._save_state()
            logger.info("[level-isolation] state reset")
            return self.get_detail()


level_isolated_instrument_service = LevelIsolatedInstrumentService()