"""STRATEGY 2 — Stage 3 live watcher thread.

A background thread that:

    1. Reads Stage 2 isolated contracts from disk (once per second).
    2. Reads each contract's own isolation level (S3 by default) from
       its opening_range.json.
    3. Polls live LTP via live_feed_service.get_latest_ltp().
    4. Records the FIRST touch of the isolation level per contract via
       strategy_2_isolation_service.record_touch().
    5. Calls strategy_2_isolation_service.run() to refresh the summary
       whenever a new touch is detected (so Stage 4 can act on it).
    6. Sends a Telegram notification when the isolated pick changes.

Window: STRATEGY_2_TRACKER_WINDOW_START (09:15) → _END (15:30).
Polling: STRATEGY_2_TRACKER_POLL_SECONDS (default 1 s).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from services.strategy_2_isolation_service import (
    strategy_2_isolation_service,
    _isolation_level_key,
    _read_contract_isolation_level,
    _safe,
    _stage1_root,
    _stage2_root,
)
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = logging.getLogger(__name__)

def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")

def _market_now() -> datetime:
    return datetime.now(_market_tz())

def _parse_hhmm(text: str, default: dtime) -> dtime:
    raw = str(text or "").strip()
    parts = raw.split(":")
    if len(parts) < 2:
        return default
    try:
        return dtime(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return default

def _in_window() -> bool:
    start = _parse_hhmm(config.STRATEGY_2_TRACKER_WINDOW_START, dtime(9, 15))
    end = _parse_hhmm(config.STRATEGY_2_TRACKER_WINDOW_END, dtime(15, 30))
    now = _market_now().time()
    return start <= now <= end

def _snapshot_touched() -> set[str]:
    return {c["instrument_key"] for c in strategy_2_isolation_service.touches()}

class Strategy2LiveTrackerService:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_isolated_key: str | None = None
        self._last_scan: dict[str, Any] = {
            "stage2_contracts": 0,
            "touched_contracts": 0,
            "isolated": None,
            "updated_at": None,
        }
        self._started_at: datetime | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        if not config.STRATEGY_2_ENABLED or not config.STRATEGY_2_TRACKER_ENABLED:
            logger.info("[strategy2-tracker] disabled by config; not starting")
            return False
        if self._thread and self._thread.is_alive():
            return True

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="strategy2-tracker", daemon=True
        )
        self._thread.start()
        self._started_at = _market_now()
        logger.info(
            "[strategy2-tracker] started (window=%s-%s, poll=%ds, level=%s)",
            config.STRATEGY_2_TRACKER_WINDOW_START,
            config.STRATEGY_2_TRACKER_WINDOW_END,
            config.STRATEGY_2_TRACKER_POLL_SECONDS,
            _isolation_level_key(),
        )
        telegram_service.send_message(
            build(
                emoji="🎯",
                title="Strategy 2 · Stage 3 Tracker · Started",
                fields=[
                    ("Window", f"{config.STRATEGY_2_TRACKER_WINDOW_START} – {config.STRATEGY_2_TRACKER_WINDOW_END} IST"),
                    ("Poll", f"{config.STRATEGY_2_TRACKER_POLL_SECONDS}s"),
                    ("Level", _isolation_level_key()),
                ],
                sections=[(
                    "Watching",
                    "🔍",
                    [f"All Stage 2 isolated contracts"],
                )],
                status="running",
                status_detail="touch detection active",
            ),
            level="INFO",
        )
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("[strategy2-tracker] stopped")

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "window_start": config.STRATEGY_2_TRACKER_WINDOW_START,
            "window_end": config.STRATEGY_2_TRACKER_WINDOW_END,
            "isolation_level": _isolation_level_key(),
            "poll_seconds": config.STRATEGY_2_TRACKER_POLL_SECONDS,
            **self._last_scan,
        }

    # ------------------------------------------------------------------ #
    # Main loop                                                          #
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        from services.live_feed_service import live_feed_service

        stage2_root = _stage2_root()
        reload_interval = 30.0  # reload stage2 folder every 30 s
        last_reload = 0.0
        contracts: list[dict[str, Any]] = []

        while not self._stop.is_set():
            try:
                if not _in_window():
                    self._stop.wait(config.STRATEGY_2_TRACKER_POLL_SECONDS)
                    continue

                now = time.monotonic()
                if now - last_reload > reload_interval or not contracts:
                    contracts = self._load_contracts(stage2_root)
                    last_reload = now

                new_touch = False
                for c in contracts:
                    if c["instrument_key"] in _snapshot_touched():
                        continue  # already touched
                    if c.get("isolation_level") is None:
                        continue
                    ltp = live_feed_service.get_latest_ltp(c["instrument_key"])
                    if ltp is None:
                        continue
                    if float(ltp) <= float(c["isolation_level"]):
                        minute = _market_now().replace(
                            second=0, microsecond=0
                        ).isoformat()[:16]
                        added = strategy_2_isolation_service.record_touch(
                            instrument_key=c["instrument_key"],
                            ticker=c["ticker"],
                            index_name=c["index_name"],
                            index_key=c["index_key"],
                            strike_price=c["strike_price"],
                            strike_type=c["strike_type"],
                            level=float(c["isolation_level"]),
                            touch_ltp=float(ltp),
                            touch_minute=minute,
                        )
                        if added:
                            new_touch = True

                if new_touch:
                    self._refresh_summary()

                self._stop.wait(config.STRATEGY_2_TRACKER_POLL_SECONDS)

            except Exception:
                logger.exception("[strategy2-tracker] loop error")
                self._stop.wait(3)

    def _load_contracts(self, stage2_root: Path) -> list[dict[str, Any]]:
        if not stage2_root.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            for ticker_dir in sorted(stage2_root.iterdir()):
                if not ticker_dir.is_dir():
                    continue
                for index_dir in sorted(ticker_dir.iterdir()):
                    if not index_dir.is_dir():
                        continue
                    for strike_dir in sorted(index_dir.iterdir()):
                        if not strike_dir.is_dir():
                            continue
                        meta = self._read_json(strike_dir / "isolation_meta.json")
                        s1_meta = self._read_json(strike_dir / "strategy_meta.json")
                        instrument_key = (
                            (meta or {}).get("upstox_instrument_key")
                            or (s1_meta or {}).get("upstox_instrument_key")
                        )
                        if not instrument_key:
                            continue
                        level, _ = _read_contract_isolation_level(
                            ticker=ticker_dir.name,
                            index_name=index_dir.name,
                            strike_dir_name=strike_dir.name,
                        )
                        out.append({
                            "instrument_key": str(instrument_key),
                            "ticker": ticker_dir.name,
                            "index_name": index_dir.name,
                            "index_key": (s1_meta or {}).get(
                                "index_instrument_key", ""
                            ),
                            "strike_price": (meta or {}).get(
                                "strike_price",
                                (s1_meta or {}).get("strike_price"),
                            ),
                            "strike_type": (meta or {}).get(
                                "strike_type",
                                (s1_meta or {}).get("strike_type"),
                            ),
                            "isolation_level": level,
                        })
        except Exception:
            logger.exception("[strategy2-tracker] failed to reload contracts")
        return out

    def _refresh_summary(self) -> None:
        try:
            summary = strategy_2_isolation_service.run(
                reason="live_tracker", mode="live", catchup=False
            )
            self._last_scan = {
                "stage2_contracts": summary.get("stage2_contracts", 0),
                "touched_contracts": summary.get("touched_contracts", 0),
                "isolated": summary.get("isolated"),
                "updated_at": summary.get("generated_at"),
            }
            isolated = summary.get("isolated")
            if isolated:
                key = f"{isolated['instrument_key']}"
                if key != self._last_isolated_key:
                    self._last_isolated_key = key
                    self._notify_new_isolation(isolated)
        except Exception:
            logger.exception("[strategy2-tracker] summary refresh failed")

    def _notify_new_isolation(self, isolated: dict[str, Any]) -> None:
        try:
            telegram_service.send_message(
                build(
                    emoji="🔒",
                    title="Strategy 2 · Contract Isolated",
                    fields=[
                        ("Ticker", isolated.get("ticker")),
                        ("Strike", isolated.get("strike_price")),
                        ("Type", isolated.get("strike_type")),
                        ("Index", isolated.get("index_name")),
                        ("Level", isolated.get("isolation_level_name")),
                        ("Touch LTP", f"{isolated.get('touch_ltp')}"),
                        ("Touch At", isolated.get("touch_minute")),
                        ("Key", isolated.get("instrument_key")),
                    ],
                    sections=[(
                        "Next Step",
                        "⏭️",
                        ["Stage 4 will watch for the next EMA cross on this contract."],
                    )],
                    status="isolated",
                    status_detail="awaiting next cross for Stage 4",
                ),
                level="SUCCESS",
            )
        except Exception:
            logger.exception("[strategy2-tracker] telegram notify failed")

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            import json
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

strategy_2_live_tracker_service = Strategy2LiveTrackerService()