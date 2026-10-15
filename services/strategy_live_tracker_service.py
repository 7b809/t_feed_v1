"""Stage 3 — R2 activation tracker.

Runs as a background daemon thread. Inside the configured window
(default 09:15–09:20 IST) it polls each tracked index's live LTP and,
the first time that index reaches its R2 level, activates every
stage-2 contract belonging to that index.

Telegram notifications:
    * "Stage 3 Started"           once, when the tracker is started
    * "Stage 3 Window Open"       once, when the tracker enters the window
    * "R2 Crossed — <INDEX>"      once per index when it crosses R2,
                                   with the count of contracts activated
    * "Stage 3 Window Closed"     once, when the window ends, with a
                                   final summary
    * "Stage 3 Error"             on any unhandled loop exception
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any

from core import config
from services.live_feed_service import live_feed_service
from services.opening_range_service import opening_range_service
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)


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


def _parse_hhmm(text: str) -> dtime:
    parts = str(text or "").strip().split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return dtime(9, 15)
    return dtime(max(0, min(23, h)), max(0, min(59, m)))


class StrategyLiveTrackerService:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._activated: dict[str, dict[str, Any]] = {}
        self._contracts_by_index: dict[str, list[dict[str, Any]]] = {}
        self._loaded_day: str | None = None

        # Notification dedupe flags (reset per day).
        self._notified_window_open: str | None = None
        self._notified_window_close: str | None = None
        self._notified_index_crossed: set[str] = set()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        if not config.STRATEGY_ENABLED or not config.STRATEGY_TRACKER_ENABLED:
            logger.info("[stage_3] tracker disabled by config")
            return False
        if self._thread and self._thread.is_alive():
            return True

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="strategy-r2-tracker", daemon=True,
        )
        self._thread.start()
        logger.info(
            "[stage_3] tracker started (window %s-%s, poll %ds)",
            config.STRATEGY_TRACKER_WINDOW_START,
            config.STRATEGY_TRACKER_WINDOW_END,
            config.STRATEGY_TRACKER_POLL_SECONDS,
        )
        telegram_service.send_message(
            "Strategy Stage 3 Started",
            (
                f"R2 activation tracker running.\n"
                f"Window: {config.STRATEGY_TRACKER_WINDOW_START}–"
                f"{config.STRATEGY_TRACKER_WINDOW_END}\n"
                f"Watching: {', '.join(config.STRATEGY_TRACKER_INDEX_KEYS or [])}"
            ),
            level="INFO",
        )
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("[stage_3] tracker stopped")

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "window_start": config.STRATEGY_TRACKER_WINDOW_START,
            "window_end": config.STRATEGY_TRACKER_WINDOW_END,
            "in_window": self._in_window(),
            "loaded_day": self._loaded_day,
            "tracked_indexes": list(self._contracts_by_index.keys()),
            "activated_count": len(self._activated),
        }

    # ------------------------------------------------------------------ #
    # Main loop                                                          #
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                today = datetime.now().date().isoformat()
                if self._loaded_day != today:
                    self._reset_daily_flags()
                    self._load_stage2(today)

                in_window = self._in_window()
                if in_window:
                    self._maybe_notify_window_open(today)
                    self._tick(today)
                else:
                    self._maybe_notify_window_close(today)

                self._stop.wait(config.STRATEGY_TRACKER_POLL_SECONDS)
            except Exception as exc:
                logger.exception("[stage_3] tracker loop error")
                try:
                    telegram_service.send_message(
                        "Strategy Stage 3 Error",
                        f"{type(exc).__name__}: {exc}",
                        level="ERROR",
                    )
                except Exception:
                    pass
                self._stop.wait(2)

    def _reset_daily_flags(self) -> None:
        self._notified_window_open = None
        self._notified_window_close = None
        self._notified_index_crossed = set()

    def _in_window(self) -> bool:
        now = datetime.now().time()
        start = _parse_hhmm(config.STRATEGY_TRACKER_WINDOW_START)
        end = _parse_hhmm(config.STRATEGY_TRACKER_WINDOW_END)
        return start <= now <= end

    # ------------------------------------------------------------------ #
    # Notifications                                                      #
    # ------------------------------------------------------------------ #
    def _maybe_notify_window_open(self, today: str) -> None:
        if self._notified_window_open == today:
            return
        self._notified_window_open = today
        total = sum(len(v) for v in self._contracts_by_index.values())
        telegram_service.send_message(
            "Strategy Stage 3 Window Open",
            (
                f"R2 tracking window started.\n"
                f"Watching {len(self._contracts_by_index)} indexes / "
                f"{total} contracts."
            ),
            level="INFO",
        )

    def _maybe_notify_window_close(self, today: str) -> None:
        if self._notified_window_close == today:
            return
        # Only fire after we've already announced the window open today.
        if self._notified_window_open != today:
            return
        self._notified_window_close = today
        telegram_service.send_message(
            "Strategy Stage 3 Window Closed",
            (
                f"R2 tracking window ended.\n"
                f"Activated contracts: {len(self._activated)}\n"
                f"Indexes that crossed R2: "
                f"{len(self._notified_index_crossed)}"
                + (
                    " (" + ", ".join(sorted(self._notified_index_crossed)) + ")"
                    if self._notified_index_crossed else ""
                )
            ),
            level="SUCCESS" if self._activated else "INFO",
        )

    def _notify_index_crossed(
        self,
        index_key: str,
        r2: float,
        index_ltp: float,
        contracts_activated: int,
    ) -> None:
        if index_key in self._notified_index_crossed:
            return
        self._notified_index_crossed.add(index_key)
        telegram_service.send_message(
            f"R2 Crossed — {index_key}",
            (
                f"Index LTP: {index_ltp:.2f}\n"
                f"R2 level: {r2:.2f}\n"
                f"Contracts activated: {contracts_activated}"
            ),
            level="SUCCESS",
        )

    # ------------------------------------------------------------------ #
    # Stage-2 load                                                       #
    # ------------------------------------------------------------------ #
    def _load_stage2(self, today: str) -> None:
        stage2_base = (
            Path(config.STRATEGY_BASE_DIR) / config.STRATEGY_STAGE_2_NAME
        )
        self._contracts_by_index = {}
        if not stage2_base.exists():
            self._loaded_day = today
            return

        for ticker_dir in sorted(stage2_base.iterdir()):
            if not ticker_dir.is_dir() or ticker_dir.name.startswith("."):
                continue
            for index_dir in sorted(ticker_dir.iterdir()):
                if not index_dir.is_dir():
                    continue
                for strike_dir in sorted(index_dir.iterdir()):
                    if not strike_dir.is_dir():
                        continue
                    meta = _read_json(strike_dir / "isolation_meta.json")
                    if not meta:
                        continue
                    index_key = self._index_key_for_ticker(ticker_dir.name)
                    if not index_key:
                        continue
                    self._contracts_by_index.setdefault(index_key, []).append(
                        {
                            "instrument_key": (
                                meta.get("upstox_instrument_key")
                                or meta.get("placeholder_instrument_key")
                                or strike_dir.name
                            ),
                            "upstox_instrument_key":
                                meta.get("upstox_instrument_key"),
                            "ticker": ticker_dir.name,
                            "index_name": index_dir.name,
                            "index_key": index_key,
                            "strike_price": meta.get("strike_price"),
                            "strike_type": meta.get("strike_type"),
                            "folder": str(strike_dir),
                            "atm_strike": meta.get("atm_strike"),
                        }
                    )

        self._loaded_day = today
        logger.info(
            "[stage_3] loaded stage-2 contracts — indexes=%d total=%d",
            len(self._contracts_by_index),
            sum(len(v) for v in self._contracts_by_index.values()),
        )

    @staticmethod
    def _index_key_for_ticker(ticker: str) -> str | None:
        want = str(ticker or "").upper().strip()
        for spec in (config.STRATEGY_INDEXES or []):
            if not isinstance(spec, dict):
                continue
            t = str(spec.get("ticker") or "").upper().strip()
            if t == want:
                return str(spec.get("instrument_key") or "")
        return None

    # ------------------------------------------------------------------ #
    # Per-tick R2 check                                                  #
    # ------------------------------------------------------------------ #
    def _tick(self, today: str) -> None:
        for index_key in config.STRATEGY_TRACKER_INDEX_KEYS or []:
            index_key = str(index_key or "").strip()
            if not index_key:
                continue

            ltp = live_feed_service.get_latest_ltp(index_key)
            if ltp is None:
                continue

            or_payload = opening_range_service.get_opening_range(index_key)
            if not or_payload.get("available") or not or_payload.get("is_today"):
                continue
            r2 = (or_payload.get("levels") or {}).get("R2")
            if r2 is None:
                continue

            if ltp < float(r2):
                continue

            # R2 crossed — activate every contract of this index.
            contracts = self._contracts_by_index.get(index_key) or []
            activated_now = 0
            for c in contracts:
                if self._activate(c, index_key, float(r2), float(ltp), today):
                    activated_now += 1

            if activated_now:
                self._notify_index_crossed(
                    index_key, float(r2), float(ltp), activated_now,
                )

    def _activate(
        self,
        contract: dict[str, Any],
        index_key: str,
        r2: float,
        index_ltp: float,
        today: str,
    ) -> bool:
        key = str(contract.get("instrument_key") or "")
        if not key or key in self._activated:
            return False

        record = {
            "stage": "stage_3",
            "day": today,
            "instrument_key": key,
            "upstox_instrument_key": contract.get("upstox_instrument_key"),
            "ticker": contract.get("ticker"),
            "index_name": contract.get("index_name"),
            "index_key": index_key,
            "strike_price": contract.get("strike_price"),
            "strike_type": contract.get("strike_type"),
            "atm_strike": contract.get("atm_strike"),
            "stage_2_folder": contract.get("folder"),
            "r2_value": round(r2, 4),
            "index_ltp_at_touch": round(index_ltp, 4),
            "direction": "above_r2",
            "touched_at": _now_iso(),
        }
        self._activated[key] = record

        ticker = str(contract.get("ticker") or "UNKNOWN")
        index_name = str(contract.get("index_name") or "INDEX")
        strike_type = str(contract.get("strike_type") or "")
        strike_price = contract.get("strike_price")

        out_dir = (
            Path(config.STRATEGY_BASE_DIR)
            / config.STRATEGY_STAGE_3_NAME
            / ticker
            / index_name
            / f"{strike_price}_{strike_type}"
        )
        _write_json(out_dir / "r2_activation.json", record)
        logger.info(
            "[stage_3] activated %s (%s %s %s) — index=%s r2=%.2f ltp=%.2f",
            key, ticker, strike_price, strike_type, index_key, r2, index_ltp,
        )

        self._write_summary(today)
        return True

    def _write_summary(self, today: str) -> None:
        base = (
            Path(config.STRATEGY_BASE_DIR) / config.STRATEGY_STAGE_3_NAME
        )
        summary = {
            "status": "ok",
            "stage": "stage_3",
            "day": today,
            "generated_at": _now_iso(),
            "window_start": config.STRATEGY_TRACKER_WINDOW_START,
            "window_end": config.STRATEGY_TRACKER_WINDOW_END,
            "activated_count": len(self._activated),
            "activated": list(self._activated.values()),
        }
        _write_json(base / "stage_3_summary.json", summary)


strategy_live_tracker_service = StrategyLiveTrackerService()