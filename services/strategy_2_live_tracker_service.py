# File: services/strategy_2_live_tracker_service.py
"""STRATEGY 2 — Stage 3 live watcher thread.

A background thread that:

    1. Reads Stage 2 isolated contracts from disk (once every 30 s
       while the tree is populated, every 5 s while it is still
       empty — so it picks up Stage 2's output quickly without
       hammering the disk on quiet days).
    2. Reads each contract's own isolation level (S3 by default) from
       its ``opening_range.json`` — trying the Stage-1 strike folder
       first and the canonical ``data/index/<safe_key>`` path second
       (the same fallback ``strategy_2_isolation_service`` uses).
    3. Polls live LTP via ``live_feed_service.get_latest_ltp()``.
    4. Records the FIRST touch of the isolation level per contract via
       ``strategy_2_isolation_service.record_touch()``.
    5. Calls ``strategy_2_isolation_service.run()`` to refresh the
       summary whenever a new touch is detected (so Stage 4 can act
       on it).
    6. Sends a Telegram notification when the isolated pick changes.

Window: STRATEGY_2_TRACKER_WINDOW_START (09:15) → _END (15:30).
Polling: STRATEGY_2_TRACKER_POLL_SECONDS (default 1 s).

Diagnostics (added for the 2026-10-22 ``no_activations`` bug)
-------------------------------------------------------------
The tracker used to run silently whether or not it had usable data.
On 2026-10-22 it was started at 07:00 with an empty Stage-2 tree,
the tree populated at 09:17:00, but every contract had
``isolation_level = None`` (Stage 1 never wrote the strike's OR) and
every ``get_latest_ltp()`` returned ``None`` (new strikes were never
subscribed to the feed). The tracker dutifully skipped all 68
contracts and Stage 4 reported ``placed=0`` — with no log line to
say *why*.

This module now:

    * loads contracts on ``start()`` so the Telegram startup message
      reports the real count;
    * logs ``loaded / missing_level / ltp_available`` on every reload;
    * fires a WARNING naming the first offender when any contract has
      ``isolation_level is None``;
    * emits a heartbeat every 5 min with the live LTP coverage and
      the missing-level count;
    * surfaces ``contracts_missing_level`` / ``contracts_with_ltp`` /
      ``last_load_at`` through ``status()`` so
      ``/admin/strategy2/tracker/status`` shows the tracker's data
      health, not just that its thread is alive.

Shape tolerance and notification suppression (10:40 fix)
--------------------------------------------------------
The ``isolated`` record in ``stage_3_summary.json`` used to carry its
touch metadata only under a nested ``touch`` sub-dict::

    {
      "isolated": {
        "instrument_key": "BSE_FO|1125354",
        "ticker": "SENSEX",
        "touch": {                            <-- nested
          "touch_minute": "2026-10-22T10:40",
          "touch_ltp": 402.95,
          ...
        }
      }
    }

``strategy_2_isolation_service._pick_isolated()`` was patched on
2026-10-22 to also promote those fields to the top level (making the
shape flat), but two things can still leave a nested-only record in
play:

    * a rolling deploy (the on-disk summary is written by pre-patch
      code while the tracker is already running the post-patch code),
    * a stale summary from an earlier run that survives a process
      restart because the tracker's in-memory touches were cleared.

The 10:40 restart logs showed exactly this: two "Contract Isolated"
Telegram messages with ``Touch LTP: None`` / ``Touch At: None``,
immediately after "Application Started". The notifier was reading
``isolated["touch_ltp"]`` at the top level and getting ``None`` for
a record that did have ``touch_ltp`` nested under ``isolated["touch"]``.

This module now:

    * reads every touch-related field via ``_get_isolated_field()``,
      which checks the top level first and falls back to the nested
      ``touch`` sub-dict. Both the flat (post-fix) and nested (legacy)
      shapes yield the same values;
    * **suppresses the Telegram notification** and logs a WARNING
      when both ``touch_ltp`` and ``touch_minute`` are empty after
      tolerant reads — a malformed isolated record can no longer
      produce a misleading "Contract Isolated" message;
    * applies the same guard inside ``_refresh_summary()`` — the
      ``_last_isolated_key`` tracking uses the tolerant read so a
      malformed record does not trigger a spurious notification.

The rule is: **no data → no notification**. The log line always
fires (so the failure is visible), but the phone only sees a
"Contract Isolated" message when the record actually carries the
touch metadata required to act on it.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger
from services.strategy_2_isolation_service import (
    strategy_2_isolation_service,
    _isolation_level_key,
    _read_contract_isolation_level,
    _stage2_root,
)
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = get_logger(__file__)

# Heartbeat cadence while inside the tracker window.
_HEARTBEAT_SECONDS = 300.0

# Reload cadence. When the stage-2 tree is populated we reload every
# 30 s (cheap; enough to pick up late Stage-2 writes). When it is
# empty we reload every 5 s so the tracker catches Stage-2's first
# write quickly without hammering the disk once per poll tick.
_RELOAD_INTERVAL_POPULATED = 30.0
_RELOAD_INTERVAL_EMPTY = 5.0


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


# ---------------------------------------------------------------------------
# Tolerant reader for the isolated record (10:40 fix)
# ---------------------------------------------------------------------------
def _get_isolated_field(isolated: dict[str, Any] | None, *names: str) -> Any:
    """Return the first non-empty value of the named fields from the
    isolated record.

    Reads the top level first, then the nested ``touch`` sub-dict.
    Handles both the post-fix flat shape (top-level fields populated)
    and the legacy nested shape (fields only under ``isolated["touch"]``).

    Args:
        isolated: the ``isolated`` record from ``stage_3_summary.json``.
        *names:   one or more field names, tried in order. The first
                  that yields a non-empty value at either level wins.

    Returns ``None`` if no named field exists in either location.
    """
    if not isinstance(isolated, dict):
        return None
    touch = isolated.get("touch")
    if not isinstance(touch, dict):
        touch = {}
    for name in names:
        v = isolated.get(name)
        if v not in (None, ""):
            return v
        v = touch.get(name)
        if v not in (None, ""):
            return v
    return None


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
            # New diagnostics — see module docstring.
            "contracts_missing_level": 0,
            "contracts_with_ltp": 0,
            "last_load_at": None,
        }
        self._started_at: datetime | None = None
        self._last_heartbeat: float = 0.0

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        if not config.STRATEGY_2_ENABLED or not config.STRATEGY_2_TRACKER_ENABLED:
            logger.info("[strategy2-tracker] disabled by config; not starting")
            return False
        if self._thread and self._thread.is_alive():
            return True

        # ---- Load contracts BEFORE sending the startup message ---- #
        # This lets the Telegram message report the actual count, and
        # surfaces the "Watching 0 contracts" case (the exact state on
        # 2026-10-22 at 07:00 IST) at INFO level rather than hiding it.
        initial: list[dict[str, Any]] = []
        try:
            from services.live_feed_service import live_feed_service
            initial = self._load_contracts(_stage2_root())
            self._log_load_summary(initial, live_feed_service, context="start")
        except Exception:
            logger.exception(
                "[strategy2-tracker] initial contract load failed"
            )

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="strategy2-tracker", daemon=True
        )
        self._thread.start()
        self._started_at = _market_now()
        logger.info(
            "[strategy2-tracker] started (window=%s-%s, poll=%ds, level=%s, "
            "contracts=%d)",
            config.STRATEGY_2_TRACKER_WINDOW_START,
            config.STRATEGY_2_TRACKER_WINDOW_END,
            config.STRATEGY_2_TRACKER_POLL_SECONDS,
            _isolation_level_key(),
            len(initial),
        )

        # Startup Telegram message — report the real count instead of
        # the generic "All Stage 2 isolated contracts".
        if initial:
            watching_lines = [
                f"{len(initial)} Stage 2 isolated contract(s) loaded",
            ]
        else:
            watching_lines = [
                "Stage 2 tree is empty — waiting for Stage 2 to write "
                "contracts (09:17 IST on a normal trading day).",
            ]
            logger.warning(
                "[strategy2-tracker] started with 0 contracts — "
                "Stage 2 has not produced output yet. The tracker will "
                "reload every %.0fs until the tree is populated.",
                _RELOAD_INTERVAL_EMPTY,
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
                sections=[("Watching", "🔍", watching_lines)],
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
        last_reload = 0.0
        contracts: list[dict[str, Any]] = []
        self._last_heartbeat = time.monotonic()

        while not self._stop.is_set():
            try:
                if not _in_window():
                    self._stop.wait(config.STRATEGY_2_TRACKER_POLL_SECONDS)
                    continue

                now = time.monotonic()

                # Reload cadence: 30 s when populated, 5 s when empty.
                # Reloading every 5 s while empty means the tracker
                # catches Stage-2's first write within 5 s instead of
                # up to 30 s, without hammering the disk once per poll.
                reload_interval = (
                    _RELOAD_INTERVAL_POPULATED
                    if contracts
                    else _RELOAD_INTERVAL_EMPTY
                )
                if now - last_reload > reload_interval:
                    contracts = self._load_contracts(stage2_root)
                    last_reload = now
                    self._log_load_summary(
                        contracts, live_feed_service, context="reload",
                    )

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

                # Periodic heartbeat every 5 min — cheap, and the one
                # line an operator can grep for to confirm the tracker
                # is alive with real data.
                if now - self._last_heartbeat > _HEARTBEAT_SECONDS:
                    self._last_heartbeat = now
                    self._heartbeat(contracts, live_feed_service)

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
                            # Forward the key so the canonical
                            # data/index/<safe_key>/opening_range.json
                            # fallback can fire when the Stage-1 strike
                            # folder copy is missing.
                            instrument_key=str(instrument_key),
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

    # ------------------------------------------------------------------ #
    # Diagnostics                                                        #
    # ------------------------------------------------------------------ #
    def _log_load_summary(
        self,
        contracts: list[dict[str, Any]],
        live_feed_service: Any,
        *,
        context: str,
    ) -> None:
        """Log a one-line load summary and update ``self._last_scan``.

        Fires on every reload (``context="reload"``) and once at
        startup (``context="start"``). The WARNING branch names the
        first contract whose isolation level is missing — the exact
        diagnostic missing from the 2026-10-22 log stream.
        """
        total = len(contracts)
        missing_level = [
            c for c in contracts if c.get("isolation_level") is None
        ]

        ltp_available = 0
        for c in contracts:
            try:
                if live_feed_service.get_latest_ltp(c["instrument_key"]) is not None:
                    ltp_available += 1
            except Exception:
                # get_latest_ltp is a dict lookup — should never raise,
                # but a diagnostic must not break the tracker.
                continue

        self._last_scan["stage2_contracts"] = total
        self._last_scan["contracts_missing_level"] = len(missing_level)
        self._last_scan["contracts_with_ltp"] = ltp_available
        self._last_scan["last_load_at"] = _market_now().isoformat()

        logger.info(
            "[strategy2-tracker] %s — contracts=%d missing_level=%d "
            "ltp_available=%d",
            context,
            total,
            len(missing_level),
            ltp_available,
        )

        if missing_level:
            first = missing_level[0]
            logger.warning(
                "[strategy2-tracker] %d/%d contract(s) have NO isolation "
                "level — they will be skipped by the touch detector. "
                "First offender: ticker=%s index=%s strike=%s_%s key=%s. "
                "Check that the Stage-1 strike folder OR the canonical "
                "data/index/<safe_key> folder contains opening_range.json "
                "with levels.%s.",
                len(missing_level),
                total,
                first.get("ticker"),
                first.get("index_name"),
                first.get("strike_price"),
                first.get("strike_type"),
                first.get("instrument_key"),
                _isolation_level_key(),
            )

        # LTP coverage warning — the other half of the 2026-10-22 bug.
        if total > 0 and ltp_available == 0:
            logger.warning(
                "[strategy2-tracker] %d contract(s) loaded but NONE has "
                "a live LTP — the touch detector cannot fire. The strike "
                "keys are probably not subscribed to the live feed. "
                "Verify with: GET /live/status (instrument_count should "
                "equal the registry count). If they are missing, the "
                "Stage-1 live-feed subscription did not run for this "
                "batch.",
                total,
            )
        elif total > 0 and 0 < ltp_available < total:
            logger.info(
                "[strategy2-tracker] %d/%d contract(s) have a live LTP "
                "(the remainder have not ticked yet).",
                ltp_available, total,
            )

    def _heartbeat(
        self,
        contracts: list[dict[str, Any]],
        live_feed_service: Any,
    ) -> None:
        """Emit a single INFO line every 5 min so an operator can see
        the tracker is alive and what its view of the world is."""
        touched = _snapshot_touched()
        ltp_available = 0
        missing_level = 0
        for c in contracts:
            if c.get("isolation_level") is None:
                missing_level += 1
            try:
                if live_feed_service.get_latest_ltp(c["instrument_key"]) is not None:
                    ltp_available += 1
            except Exception:
                continue
        logger.info(
            "[strategy2-tracker] heartbeat — contracts=%d touched=%d "
            "missing_level=%d ltp_available=%d isolated=%s",
            len(contracts),
            len(touched & {c["instrument_key"] for c in contracts}),
            missing_level,
            ltp_available,
            (self._last_scan.get("isolated") or {}).get("instrument_key")
            if isinstance(self._last_scan.get("isolated"), dict)
            else "none",
        )

    # ------------------------------------------------------------------ #
    # Summary refresh + notification                                     #
    # ------------------------------------------------------------------ #
    def _refresh_summary(self) -> None:
        try:
            summary = strategy_2_isolation_service.run(
                reason="live_tracker", mode="live", catchup=False
            )
            self._last_scan["stage2_contracts"] = summary.get(
                "stage2_contracts", self._last_scan.get("stage2_contracts", 0)
            )
            self._last_scan["touched_contracts"] = summary.get(
                "touched_contracts", self._last_scan.get("touched_contracts", 0)
            )
            self._last_scan["isolated"] = summary.get("isolated")
            self._last_scan["updated_at"] = summary.get("generated_at")
            # Propagate the isolation service's own missing-level count
            # so the two views never diverge. If the tracker loaded a
            # contract that the service couldn't read an OR for, both
            # surfaces report it.
            if "contracts_missing_level" in summary:
                self._last_scan["contracts_missing_level"] = summary[
                    "contracts_missing_level"
                ]

            isolated = summary.get("isolated")
            if isolated:
                # ---- Shape-aware isolated key ---------------------- #
                # ``_get_isolated_field`` reads both the flat (post-fix)
                # and nested (legacy) shapes. A malformed record that
                # has no readable key must not be treated as a new
                # isolated pick.
                key = str(
                    _get_isolated_field(isolated, "instrument_key") or ""
                ).strip()
                if not key:
                    logger.warning(
                        "[strategy2-tracker] isolated record has no "
                        "readable instrument_key at the top level or "
                        "under isolated['touch']. Record shape may be "
                        "malformed. Skipping notification. record=%r",
                        isolated,
                    )
                    return
                if key != self._last_isolated_key:
                    self._last_isolated_key = key
                    self._notify_new_isolation(isolated)
        except Exception:
            logger.exception("[strategy2-tracker] summary refresh failed")

    def _notify_new_isolation(self, isolated: dict[str, Any]) -> None:
        """Send the "Contract Isolated" Telegram notification.

        Reads every touch-related field through ``_get_isolated_field``
        so both the flat (post-fix) and nested (legacy) shapes work.
        **Suppresses the notification** and logs a WARNING when both
        ``touch_ltp`` and ``touch_minute`` are empty after tolerant
        reads — the 10:40 symptom was a "Contract Isolated" message
        with ``Touch LTP: None`` for a record that had ``touch_ltp``
        nested under ``isolated["touch"]``. That notification was
        useless to act on; the log line is enough.
        """
        try:
            touch_ltp = _get_isolated_field(isolated, "touch_ltp")
            touch_minute = _get_isolated_field(isolated, "touch_minute")

            # ---- Suppress when touch metadata is missing --------- #
            if touch_ltp is None and not touch_minute:
                logger.warning(
                    "[strategy2-tracker] suppressing isolated "
                    "notification for %s — touch_ltp=%r touch_minute=%r. "
                    "The record is missing touch metadata; this is a "
                    "stale/persisted summary, not a live touch. Run "
                    "POST /admin/strategy2/stage3/run to rebuild the "
                    "summary from the current tracker state.",
                    _get_isolated_field(isolated, "instrument_key"),
                    touch_ltp,
                    touch_minute,
                )
                return

            telegram_service.send_message(
                build(
                    emoji="🔒",
                    title="Strategy 2 · Contract Isolated",
                    fields=[
                        ("Ticker", _get_isolated_field(isolated, "ticker")),
                        (
                            "Strike",
                            _get_isolated_field(isolated, "strike_price"),
                        ),
                        (
                            "Type",
                            _get_isolated_field(isolated, "strike_type"),
                        ),
                        (
                            "Index",
                            _get_isolated_field(isolated, "index_name"),
                        ),
                        (
                            "Level",
                            _get_isolated_field(
                                isolated, "isolation_level_name"
                            ),
                        ),
                        ("Touch LTP", f"{touch_ltp}"),
                        ("Touch At", touch_minute),
                        (
                            "Key",
                            _get_isolated_field(isolated, "instrument_key"),
                        ),
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