# services/strategy_live_tracker_service.py
"""Stage 3 — R2 activation tracker.

Runs as a background daemon thread. Inside the configured window
(default 09:15–09:20 IST) it polls each tracked index's live LTP and,
the first time that index reaches its R2 level, activates every
stage-2 contract belonging to that index.

Reload behaviour (this revision — cron-flow fix)
------------------------------------------------
Historically the tracker cached the Stage-2 contract list **once** at
``start()`` and never reloaded. On the normal cron-only flow
(engine launched before 09:00, ``strategy_isolation_calc`` writes the
stage-2 tree at 09:17:00, ``strategy_order_calc`` reads the stage-3
summary at 09:18:00), the cache was still empty at Stage-4 time, so
the tracker never activated anything and Stage 4 reported
``no_activations``.

This revision mirrors the Strategy-2 tracker's design:

    * ``_run()`` reloads the Stage-2 tree from disk **every 30 s**
      (and immediately whenever the in-memory cache is empty) so a
      Stage-2 write that happens after ``start()`` is picked up long
      before Stage-4 fires.
    * ``run_catchup()`` **unconditionally reloads** the Stage-2 tree
      from disk at the top of the call, so a caller that invokes it
      right after Stage 2 (e.g. ``token_tasks/strategy_isolation_task``)
      is guaranteed to see the freshly-written contracts.

Existing activations are preserved across reloads: ``_load_stage2``
only resets ``self._contracts_by_index``, never ``self._activated``.

Simulation mode (catch-up) — PER-CONTRACT EVALUATION, WINDOW-BOUNDED
-------------------------------------------------------------------
``run_catchup(mode, catchup)`` is a **one-shot** helper called from the
unified refresh routine when the project starts (or daily / hard
refresh fires) after the window has already closed.

In this mode, the decision is made **per option contract**, and every
measurement is **strictly bounded by the strategy-defined window**
(``STRATEGY_TRACKER_WINDOW_START`` → ``STRATEGY_TRACKER_WINDOW_END``,
default ``09:15`` → ``09:20`` IST).

**Candle isolation rule (IMPORTANT):**

    The Upstox intraday endpoint returns the whole day's tape — there
    is no query parameter for a time slice. This service therefore
    slices the response **inside the fetch helper** so that the moment
    candles leave ``_fetch_intraday_candles`` they are already
    restricted to ``window_start_minute`` ≤ minute ≤
    ``window_end_minute``. Every downstream consumer — R2 computation,
    window-high comparison, index context, contract decisions — works
    on that pre-filtered list. There is no code path left that can see
    a candle outside the window.

    ``_candles_in_window`` is kept as a defensive second filter so
    that even a future caller cannot accidentally bypass the window.

For each stage-2 contract we:

    1. Fetch the contract's own 1-minute intraday candles from Upstox,
       already sliced to the strategy window at fetch time.

    2. Locate the contract's own **09:15 opening-range candle** (the
       first candle of the strategy window).

    3. Compute the contract's own **R2** level from that single candle
       using the standard opening-range formula
       (``R2 = H + (H - A)`` where ``A = (H + L) / 2``).

    4. Compute the contract's own **window high** by taking the maximum
       high across the already-sliced candle list.

    5. Activate the contract **if and only if**
       ``contract_window_high >= contract_R2``.

The parent index is still recorded in the output for grouping /
context, but it is **no longer** the source of the activation
decision. Each contract stands on its own intraday tape, already
sliced to the same strategy window.

Per-index aggregates (for the dashboard header)
-----------------------------------------------
Alongside the per-contract list, each per-index entry now carries:

    decision_basis           : always "per_contract" in catch-up mode
    contracts_reached_r2     : number of contracts whose own window
                               high >= their own R2
    best_contract_gap_to_r2  : max(contract_window_high - contract_R2)
                               across all contracts; positive when at
                               least one contract exceeded its own R2
    best_contract            : the contract that produced that best gap

The index's own R2 / window high / gap are still recorded under
``index_r2_value`` / ``index_window_high`` / ``index_gap_to_r2``, but
they are labelled as **informational context only** — the dashboard
shows them on a secondary line so they cannot be mistaken for the
decision basis.

Per-contract audit trail
------------------------
Every catch-up pass writes ``stage_3_decisions.json`` under the stage-3
root. It lists every stage-2 contract with:

    status                     : "activated" | "skipped"
    reason_code                : contract_reached_r2 | contract_high_below_r2 |
                                 no_opening_candle | no_candles_in_window |
                                 already_activated | activation_rejected
    reason                     : plain-English sentence with the exact numbers
    contract_r2                : the contract's own R2 level
    contract_window_high       : the contract's own window max high
    contract_window_low        : the contract's own window min low
    contract_0915_high / _low  : the contract's own 09:15 candle H / L
    gap_to_r2                  : contract_window_high - contract_r2
    window_start / window_end  : the strategy window (default 09:15 / 09:20)
    contract_candles           : how many 1-min candles fell in the window
    contract_candles_source    : "intraday_rest" | "none"

The file is written **even when 0 contracts activate**, so the
dashboard can always explain why Stage 3 produced no activations.

Telegram notifications:
    * "Stage 3 Started"           once, when the tracker is started
    * "Stage 3 Window Open"       once, when the tracker enters the window
    * "R2 Crossed — <INDEX>"      once per index when it crosses R2 (live)
    * "R2 Catch-up — <INDEX>"     once per index activated in catch-up
    * "Stage 3 Window Closed"     once, when the window ends
    * "Stage 3 Error"             on any unhandled loop exception
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from core import config
from services.live_ema_engine import live_ema_engine
from services.live_feed_service import live_feed_service
from services.opening_range_service import opening_range_service
from services.strategy_meta import write_meta
from services.telegram_service import telegram_service
from services.token_service import token_service

logger = logging.getLogger(__name__)

# Cash-market close in the market timezone. Anything after this is
# recorded as post-market in the activation record / meta test file.
_MARKET_CLOSE_TIME = dtime(15, 30)

# Upstox REST endpoint used to fetch today's intraday tape.
_UPSTOX_BASE = "https://api.upstox.com"
_HTTP_TIMEOUT = 20


# --------------------------------------------------------------------------- #
# Small helpers                                                                #
# --------------------------------------------------------------------------- #
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


def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _market_now() -> datetime:
    return datetime.now(_market_tz())


def _post_market() -> bool:
    """True after the Indian cash-market close (15:30 IST)."""
    return _market_now().time() > _MARKET_CLOSE_TIME


def _parse_hhmm(text: str) -> dtime:
    parts = str(text or "").strip().split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return dtime(9, 15)
    return dtime(max(0, min(23, h)), max(0, min(59, m)))


def _normalise_hhmm(text: str, default: str) -> str:
    """Return a ``HH:MM`` string from ``text`` (or the default)."""
    raw = str(text or "").strip() or default
    parts = raw.split(":")
    if len(parts) < 2:
        return default
    try:
        h = int(parts[0])
        m = int(parts[1])
    except (ValueError, IndexError):
        return default
    return f"{h:02d}:{m:02d}"


def _window_minute_bounds(today: str) -> tuple[str, str]:
    """Return ISO minute keys for the STRATEGY tracker window on ``today``.

    These are the ONLY minute keys ever returned by
    ``_fetch_intraday_candles`` for index or contract data, and the
    ONLY minute keys considered when computing window highs / lows.

    Default window (config): 09:15 → 09:20 IST.
    """
    start = _normalise_hhmm(config.STRATEGY_TRACKER_WINDOW_START, "09:15")
    end = _normalise_hhmm(config.STRATEGY_TRACKER_WINDOW_END, "09:20")
    return f"{today}T{start}", f"{today}T{end}"


def _opening_minute_key(today: str) -> str:
    """Return the ISO minute key for the opening-range candle."""
    hhmm = _normalise_hhmm(
        getattr(config, "OPENING_RANGE_FIRST_MINUTE", "09:15"),
        "09:15",
    )
    return f"{today}T{hhmm}"


def _short_key(key: str) -> str:
    s = str(key or "")
    return s if len(s) <= 40 else s[:20] + "…" + s[-16:]


# --------------------------------------------------------------------------- #
# Opening-range math (mirrors services/opening_range_service.compute_levels)   #
# --------------------------------------------------------------------------- #
def _compute_r2_from_candle(high: Any, low: Any) -> float | None:
    """Return R2 = H + (H - A) where A = (H + L) / 2.

    Returns None when ``high`` / ``low`` are not numeric.
    """
    try:
        h = float(high)
        l = float(low)
    except (TypeError, ValueError):
        return None
    a = (h + l) / 2.0
    d_h = abs(h - a)
    return h + d_h


# --------------------------------------------------------------------------- #
# Intraday REST fetch — WINDOW-BOUNDED at the source                          #
# --------------------------------------------------------------------------- #
def _fetch_intraday_candles(
    instrument_key: str,
    token: str | None = None,
    *,
    window_start_minute: str | None = None,
    window_end_minute: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch today's 1-min intraday candles directly from Upstox.

    IMPORTANT — candle isolation happens HERE.

    The Upstox endpoint returns the WHOLE day's tape (there is no query
    parameter for a time slice). This function therefore slices the
    response the moment it arrives, keeping ONLY candles whose
    ``minute`` key falls inside the inclusive range
    ``[window_start_minute, window_end_minute]``.

    Every caller downstream (R2 computation, window-high comparison,
    index context, contract decisions) sees only in-window candles.
    There is no code path that can see an out-of-window candle.

    When ``window_start_minute`` / ``window_end_minute`` are omitted
    (e.g. ad-hoc debugging calls), the function falls back to the
    strategy tracker window for ``today`` so the invariant still holds.
    """
    key = str(instrument_key or "").strip()
    if not key:
        return []

    if token is None:
        try:
            token = token_service.get_access_token()
        except Exception:
            token = None
    if not token:
        return []

    # Fallback: derive the window from config so we never return raw
    # whole-day data even if a caller forgot to pass bounds.
    if not window_start_minute or not window_end_minute:
        today = _market_now().date().isoformat()
        window_start_minute, window_end_minute = _window_minute_bounds(today)

    url = (
        f"{_UPSTOX_BASE}/v3/historical-candle/intraday/"
        f"{key}/minutes/1"
    )
    try:
        resp = requests.get(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException:
        logger.warning(
            "[stage_3] intraday REST fetch failed (network) for %s", key,
        )
        return []

    if resp.status_code >= 400:
        logger.info(
            "[stage_3] intraday REST fetch HTTP %s for %s: %s",
            resp.status_code, key, resp.text[:160],
        )
        return []

    try:
        body = resp.json()
    except ValueError:
        return []

    raw = (
        (body.get("data") or {}).get("candles")
        if isinstance(body.get("data"), dict)
        else body.get("candles")
    ) or []
    if not isinstance(raw, list):
        return []

    # ---- Slice at the source: only in-window candles leave here ---- #
    window_start = str(window_start_minute or "")
    window_end = str(window_end_minute or "")
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            ts_str = str(row[0])
            minute = ts_str[:16]  # "YYYY-MM-DDTHH:MM"
        except Exception:
            continue
        if not minute:
            continue
        # Minute keys are zero-padded ISO strings → lexicographic
        # comparison is correct.
        if not (window_start <= minute <= window_end):
            continue
        try:
            out.append({
                "minute": minute,
                "timestamp": ts_str,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if len(row) >= 6 else 0.0,
            })
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda c: c["minute"])
    return out


def _candles_in_window(
    candles: list[dict[str, Any]],
    window_start_minute: str,
    window_end_minute: str,
) -> list[dict[str, Any]]:
    """Defensive second filter — kept as a belt-and-braces guard.

    ``_fetch_intraday_candles`` already slices at the source, so any
    list it returns is guaranteed to satisfy the window. This helper
    exists so that even a future caller that assembled candles some
    other way cannot accidentally bypass the window.
    """
    start = str(window_start_minute or "")
    end = str(window_end_minute or "")
    if not start or not end:
        return []

    out: list[dict[str, Any]] = []
    for c in candles:
        minute = str(c.get("minute") or "")
        if not minute:
            continue
        if start <= minute <= end:
            out.append(c)
    return out


def _window_extremes(
    window_candles: list[dict[str, Any]],
) -> tuple[float | None, float | None]:
    """Return (high, low) across a window; (None, None) when empty."""
    if not window_candles:
        return None, None
    highs: list[float] = []
    lows: list[float] = []
    for c in window_candles:
        try:
            highs.append(float(c.get("high") or 0))
            lows.append(float(c.get("low") or 0))
        except (TypeError, ValueError):
            continue
    if not highs:
        return None, None
    return max(highs), min(lows)


def _find_opening_candle(
    candles: list[dict[str, Any]],
    opening_minute: str,
) -> dict[str, Any] | None:
    """Return the contract's own opening-range candle (minute == opening_minute).

    In practice ``opening_minute`` is ``"YYYY-MM-DDT09:15"`` — the left
    edge of the strategy window. Because the fetch is already
    window-bounded, this candle is guaranteed to be in the list when
    it exists in the tape.
    """
    for c in candles:
        if str(c.get("minute") or "") == opening_minute:
            return c
    return None


def _fetch_contract_candles_batch(
    contracts: list[dict[str, Any]],
    token: str | None,
    *,
    window_start_minute: str,
    window_end_minute: str,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch intraday candles for a batch of Stage-2 contracts.

    Every fetch is bounded to the strategy window at the source
    (``_fetch_intraday_candles`` slices internally). Returns a mapping
    from instrument_key -> in-window candle list. Missing / failed
    fetches simply don't appear in the map. Runs the fetches in
    parallel up to ``STRATEGY_MAX_WORKERS``.
    """
    keys: list[str] = []
    for c in contracts:
        k = (
            c.get("upstox_instrument_key")
            or c.get("instrument_key")
        )
        if k:
            keys.append(str(k))
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    if not unique:
        return {}

    workers = min(
        max(1, int(config.STRATEGY_MAX_WORKERS or 6)),
        max(1, len(unique)),
    )
    results: dict[str, list[dict[str, Any]]] = {}

    def _one(key: str) -> tuple[str, list[dict[str, Any]]]:
        return key, _fetch_intraday_candles(
            key,
            token=token,
            window_start_minute=window_start_minute,
            window_end_minute=window_end_minute,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, k) for k in unique]
        for fut in as_completed(futures):
            try:
                k, candles = fut.result()
                results[k] = candles
            except Exception:
                continue
    return results


# --------------------------------------------------------------------------- #
# Service                                                                      #
# --------------------------------------------------------------------------- #
class StrategyLiveTrackerService:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._activated: dict[str, dict[str, Any]] = {}
        self._contracts_by_index: dict[str, list[dict[str, Any]]] = {}
        self._loaded_day: str | None = None

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
            "[stage_3] tracker started (window %s-%s IST, poll %ds)",
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
            "post_market": _post_market(),
            "loaded_day": self._loaded_day,
            "tracked_indexes": list(self._contracts_by_index.keys()),
            "activated_count": len(self._activated),
        }

    # ------------------------------------------------------------------ #
    # Main loop (live path — index LTP vs index R2, window-gated)        #
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        # Mirror the S2 tracker's ``reload_interval`` pattern so a
        # Stage-2 write that happens *after* ``start()`` is picked up
        # within ~30 s — before the 09:18 Stage-4 order cron fires.
        #
        # Without this, the tracker keeps the empty Stage-2 cache it
        # captured at ``start()`` (e.g. 07:00 launch), Stage 4 at
        # 09:18:00 reads an empty stage_3_summary.json, and no trades
        # are placed.
        reload_interval = 30.0
        last_reload = 0.0

        while not self._stop.is_set():
            try:
                today = _market_now().date().isoformat()

                if self._loaded_day != today:
                    # Day rollover: reset flags + full reload.
                    self._reset_daily_flags()
                    self._load_stage2(today)
                    last_reload = time.monotonic()
                else:
                    # Same day: reload on schedule (30 s) or immediately
                    # when the in-memory cache is empty (Stage 2 not
                    # yet written, or last run produced 0 contracts).
                    now_mono = time.monotonic()
                    if (
                        (now_mono - last_reload) > reload_interval
                        or not self._contracts_by_index
                    ):
                        self._load_stage2(today)
                        last_reload = now_mono

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
        now = _market_now().time()
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
    # Live tick path (index-driven; window-gated)                        #
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

            contracts = self._contracts_by_index.get(index_key) or []
            activated_now = 0
            for c in contracts:
                if self._activate(
                    c, index_key, float(r2), float(ltp), today,
                    mode="live", catchup=False, post_market=False,
                    activation_method="live",
                ):
                    activated_now += 1

            if activated_now:
                self._notify_index_crossed(
                    index_key, float(r2), float(ltp), activated_now,
                )

    # ------------------------------------------------------------------ #
    # Catch-up (one-shot simulation) — PER-CONTRACT, WINDOW-BOUNDED      #
    # ------------------------------------------------------------------ #
    def run_catchup(
        self,
        *,
        mode: str = "startup",
        catchup: bool = True,
    ) -> dict[str, Any]:
        if not config.STRATEGY_ENABLED or not config.STRATEGY_TRACKER_ENABLED:
            return {
                "status": "disabled",
                "catchup": False,
                "post_market": _post_market(),
                "activated_total": 0,
                "indexes": [],
            }

        today = _market_now().date().isoformat()

        # Always reload from disk so a Stage-2 completion that happened
        # AFTER ``start()`` — or after the previous tracker tick — is
        # picked up before we evaluate the contracts. Without this, a
        # caller that invokes ``run_catchup()`` right after Stage 2
        # would still be looking at the empty cache captured at start().
        self._load_stage2(today)

        post_market = _post_market()
        window_start_minute, window_end_minute = _window_minute_bounds(today)
        opening_minute = _opening_minute_key(today)

        logger.info(
            "[stage_3] catch-up (per-contract, window-bounded) starting — "
            "mode=%s day=%s window=[%s .. %s] opening_candle=%s "
            "post_market=%s",
            mode, today,
            window_start_minute, window_end_minute,
            opening_minute, post_market,
        )
        logger.info(
            "[stage_3] candle isolation rule: intraday fetch is sliced "
            "AT THE SOURCE to '%s' <= minute <= '%s'. Downstream code "
            "never sees an out-of-window candle.",
            window_start_minute, window_end_minute,
        )

        # Resolve the token once — every REST fetch in this pass reuses it.
        try:
            token = token_service.get_access_token()
        except Exception:
            token = None

        per_index_decisions: list[dict[str, Any]] = []
        activated_total = 0

        for index_key in config.STRATEGY_TRACKER_INDEX_KEYS or []:
            index_key = str(index_key or "").strip()
            if not index_key:
                continue

            ticker, index_name = self._ticker_and_name_for(index_key)
            contracts = self._contracts_by_index.get(index_key) or []

            # ---- Index-level context (informational only) ------------ #
            index_r2_value: float | None = None
            index_window_high: float | None = None
            index_window_low: float | None = None
            index_gap_to_r2: float | None = None
            index_candles_in_window = 0
            index_candles_source = "none"

            try:
                or_payload = opening_range_service.get_opening_range(index_key)
                if or_payload.get("available") and or_payload.get("is_today"):
                    r2_idx = (or_payload.get("levels") or {}).get("R2")
                    if r2_idx is not None:
                        index_r2_value = float(r2_idx)
            except Exception:
                logger.exception(
                    "[stage_3] failed loading index OR for %s", index_key,
                )

            try:
                # Window-bounded fetch: only [09:15 .. 09:20] comes back.
                index_candles = _fetch_intraday_candles(
                    index_key,
                    token=token,
                    window_start_minute=window_start_minute,
                    window_end_minute=window_end_minute,
                )
                if index_candles:
                    index_candles_source = "intraday_rest"
                else:
                    # Fallback: the EMA engine buffer, still filtered to
                    # the strategy window by _candles_in_window below.
                    engine_payload = live_ema_engine.get_candles(
                        index_key, limit=0,
                    )
                    index_candles = engine_payload.get("candles") or []
                    if index_candles:
                        index_candles_source = "engine_buffer"

                # Defensive second filter (fetch already sliced).
                index_window = _candles_in_window(
                    index_candles, window_start_minute, window_end_minute,
                )
                index_candles_in_window = len(index_window)
                index_window_high, index_window_low = _window_extremes(index_window)
                if (
                    index_r2_value is not None
                    and index_window_high is not None
                ):
                    index_gap_to_r2 = index_window_high - index_r2_value
            except Exception:
                logger.exception(
                    "[stage_3] failed loading index intraday for %s", index_key,
                )

            # ---- Fetch every contract's own intraday tape (parallel) -- #
            # Same window bounds — every contract's tape is pre-sliced.
            contract_candles_map = _fetch_contract_candles_batch(
                contracts,
                token,
                window_start_minute=window_start_minute,
                window_end_minute=window_end_minute,
            )

            contract_decisions: list[dict[str, Any]] = []
            activated_now = 0

            for c in contracts:
                ckey = str(
                    c.get("upstox_instrument_key")
                    or c.get("instrument_key")
                    or ""
                )
                c_candles = contract_candles_map.get(ckey) or []

                decision = self._decide_for_contract_own(
                    contract=c,
                    contract_candles=c_candles,
                    index_key=index_key,
                    opening_minute=opening_minute,
                    window_start_minute=window_start_minute,
                    window_end_minute=window_end_minute,
                    today=today,
                    mode=mode,
                    catchup=True,
                    post_market=post_market,
                    index_r2_value=index_r2_value,
                    index_window_high=index_window_high,
                    index_candles_source=index_candles_source,
                )
                contract_decisions.append(decision)
                if decision.get("activated"):
                    activated_now += 1

            activated_total += activated_now
            contracts_skipped = len(contracts) - activated_now

            # ---- Per-contract aggregates for the audit header -------- #
            best_contract_gap: float | None = None
            best_contract: dict[str, Any] | None = None
            for d in contract_decisions:
                g = d.get("contract_gap_to_r2")
                if g is None:
                    continue
                if best_contract_gap is None or g > best_contract_gap:
                    best_contract_gap = g
                    best_contract = {
                        "instrument_key": d.get("instrument_key"),
                        "upstox_instrument_key":
                            d.get("upstox_instrument_key"),
                        "strike_price": d.get("strike_price"),
                        "strike_type": d.get("strike_type"),
                        "contract_gap_to_r2": g,
                        "contract_window_high":
                            d.get("contract_window_high"),
                        "contract_r2": d.get("contract_r2"),
                        "contract_0915_high": d.get("contract_0915_high"),
                        "contract_0915_low": d.get("contract_0915_low"),
                    }

            # Per-index status: "activated" if any contract activated.
            if activated_now > 0:
                index_status = "activated"
                index_reason_code = "one_or_more_contracts_activated"
                index_reason = (
                    f"{index_key}: {activated_now} of {len(contracts)} "
                    f"contracts reached their own R2 within "
                    f"{config.STRATEGY_TRACKER_WINDOW_START}–"
                    f"{config.STRATEGY_TRACKER_WINDOW_END} IST."
                )
            else:
                index_status = "skipped"
                if not contracts:
                    index_reason_code = "no_contracts"
                    index_reason = (
                        f"{index_key}: no stage-2 contracts available."
                    )
                else:
                    index_reason_code = "no_contract_reached_r2"
                    index_reason = (
                        f"{index_key}: none of the {len(contracts)} contracts "
                        f"reached their own R2 within "
                        f"{config.STRATEGY_TRACKER_WINDOW_START}–"
                        f"{config.STRATEGY_TRACKER_WINDOW_END} IST."
                    )

            per_index_decisions.append({
                "index_key": index_key,
                "ticker": ticker,
                "index_name": index_name,
                "status": index_status,
                "reason_code": index_reason_code,
                "reason": index_reason,

                # ---- decision basis (drives the dashboard header) ---- #
                "decision_basis": "per_contract",
                "contracts_reached_r2": activated_now,
                "best_contract_gap_to_r2": best_contract_gap,
                "best_contract": best_contract,

                # ---- index-level context (informational only) -------- #
                "index_r2_value": index_r2_value,
                "index_window_high": index_window_high,
                "index_window_low": index_window_low,
                "index_gap_to_r2": index_gap_to_r2,
                "index_candles_in_window": index_candles_in_window,
                "index_candles_source": index_candles_source,

                # ---- legacy aliases so existing consumers keep working #
                "r2_value": index_r2_value,
                "window_high": index_window_high,
                "gap_to_r2": index_gap_to_r2,
                "candles_in_window": index_candles_in_window,
                "candles_source": index_candles_source,

                # ---- per-contract breakdown ------------------------- #
                "window_start": window_start_minute,
                "window_end": window_end_minute,
                "contracts_total": len(contracts),
                "contracts_activated": activated_now,
                "contracts_skipped": contracts_skipped,
                "contracts": contract_decisions,
            })

            logger.info(
                "[stage_3] catch-up %s: status=%s reached_r2=%d/%d "
                "best_contract_gap=%s index_r2=%s index_window_high=%s "
                "source=%s",
                index_key, index_status, activated_now, len(contracts),
                f"{best_contract_gap:.2f}"
                if best_contract_gap is not None else "—",
                f"{index_r2_value:.2f}" if index_r2_value is not None else "—",
                f"{index_window_high:.2f}"
                if index_window_high is not None else "—",
                index_candles_source,
            )

            if activated_now:
                self._notified_index_crossed.add(index_key)
                telegram_service.send_message(
                    f"R2 Catch-up (per-contract) — {index_key}",
                    (
                        f"Mode: {mode}\n"
                        f"Simulation: yes (per-contract, window "
                        f"{config.STRATEGY_TRACKER_WINDOW_START}–"
                        f"{config.STRATEGY_TRACKER_WINDOW_END})\n"
                        f"Post-market: {post_market}\n"
                        f"Contracts activated: {activated_now}/"
                        f"{len(contracts)}"
                    ),
                    level="SUCCESS",
                )

        # ---- Summary + decisions + meta ----------------------------- #
        try:
            self._write_summary(today)
        except Exception:
            logger.exception("[stage_3] summary write failed")

        try:
            self._write_decisions_file(
                today=today,
                mode=mode,
                catchup=True,
                post_market=post_market,
                window_start_minute=window_start_minute,
                window_end_minute=window_end_minute,
                per_index_decisions=per_index_decisions,
                activated_total=activated_total,
                opening_minute=opening_minute,
            )
        except Exception:
            logger.exception("[stage_3] decisions write failed")

        self._write_catchup_meta(
            mode=mode,
            index_results=[
                {
                    "index_key": d["index_key"],
                    "status": d["status"],
                    "reason_code": d["reason_code"],
                    "decision_basis": d.get("decision_basis"),
                    "contracts_reached_r2": d.get("contracts_reached_r2"),
                    "best_contract_gap_to_r2":
                        d.get("best_contract_gap_to_r2"),
                    "index_r2": d.get("index_r2_value"),
                    "index_window_high": d.get("index_window_high"),
                    "index_candles_source": d.get("index_candles_source"),
                    "contracts_activated": d["contracts_activated"],
                    "contracts_total": d["contracts_total"],
                }
                for d in per_index_decisions
            ],
            post_market=post_market,
            window_start_minute=window_start_minute,
            window_end_minute=window_end_minute,
            opening_minute=opening_minute,
        )

        return {
            "status": "ok",
            "catchup": True,
            "simulation": True,
            "evaluation_mode": "per_contract",
            "window_start": window_start_minute,
            "window_end": window_end_minute,
            "opening_minute": opening_minute,
            "post_market": post_market,
            "mode": mode,
            "day": today,
            "activated_total": activated_total,
            "indexes": per_index_decisions,
        }

    # ------------------------------------------------------------------ #
    # Per-contract decision (per-contract, window-bounded simulation)    #
    # ------------------------------------------------------------------ #
    def _decide_for_contract_own(
        self,
        *,
        contract: dict[str, Any],
        contract_candles: list[dict[str, Any]],
        index_key: str,
        opening_minute: str,
        window_start_minute: str,
        window_end_minute: str,
        today: str,
        mode: str,
        catchup: bool,
        post_market: bool,
        index_r2_value: float | None,
        index_window_high: float | None,
        index_candles_source: str,
    ) -> dict[str, Any]:
        key = str(contract.get("instrument_key") or "")
        ticker = str(contract.get("ticker") or "?")
        strike = contract.get("strike_price")
        stype = contract.get("strike_type") or "?"

        already = key in self._activated

        # Defensive second filter: fetch already sliced at source, so
        # this is a no-op that guarantees the invariant still holds.
        c_window = _candles_in_window(
            contract_candles, window_start_minute, window_end_minute,
        )

        # ---- Locate the contract's own opening-range candle ---------- #
        opening_candle = _find_opening_candle(c_window, opening_minute)
        contract_0915_high = (
            opening_candle.get("high") if opening_candle else None
        )
        contract_0915_low = (
            opening_candle.get("low") if opening_candle else None
        )
        contract_r2 = (
            _compute_r2_from_candle(contract_0915_high, contract_0915_low)
            if opening_candle else None
        )

        # ---- Contract's own window (09:15–09:20) ------------------- #
        contract_window_high, contract_window_low = _window_extremes(c_window)
        contract_gap_to_r2: float | None = None
        if (
            contract_r2 is not None
            and contract_window_high is not None
        ):
            contract_gap_to_r2 = contract_window_high - contract_r2

        decision: dict[str, Any] = {
            "instrument_key": key,
            "upstox_instrument_key": contract.get("upstox_instrument_key"),
            "ticker": ticker,
            "index_name": contract.get("index_name"),
            "index_key": index_key,
            "strike_price": strike,
            "strike_type": stype,
            "stage_2_folder": contract.get("folder"),
            "window_start": window_start_minute,
            "window_end": window_end_minute,
            "opening_minute": opening_minute,
            "mode": mode,
            "catchup": catchup,
            "post_market": post_market,
            "decided_at": _now_iso(),
            # ---- contract's own intraday snapshot (window-bounded) -- #
            "contract_0915_high": contract_0915_high,
            "contract_0915_low": contract_0915_low,
            "contract_r2": contract_r2,
            "contract_window_high": contract_window_high,
            "contract_window_low": contract_window_low,
            "contract_gap_to_r2": contract_gap_to_r2,
            "contract_candles": len(c_window),
            "contract_candles_source": (
                "intraday_rest" if c_window else "none"
            ),
            # ---- parent-index context (informational) ------------- #
            "index_r2_value": index_r2_value,
            "index_window_high": index_window_high,
            "index_candles_source": index_candles_source,
        }

        # ---- 1. Already activated -------------------------------- #
        if already:
            decision.update({
                "status": "activated",
                "activated": True,
                "reason_code": "already_activated",
                "reason": (
                    f"{ticker} {strike} {stype}: already activated earlier "
                    f"in this session."
                ),
            })
            return decision

        # ---- 2. No opening-range candle for the contract --------- #
        if opening_candle is None:
            decision.update({
                "status": "skipped",
                "activated": False,
                "reason_code": "no_opening_candle",
                "reason": (
                    f"{ticker} {strike} {stype}: not activated because the "
                    f"contract's own {opening_minute[11:]} opening-range "
                    f"candle was not available inside the "
                    f"{config.STRATEGY_TRACKER_WINDOW_START}–"
                    f"{config.STRATEGY_TRACKER_WINDOW_END} IST window."
                ),
            })
            return decision

        # ---- 3. No window candles for the contract --------------- #
        if contract_window_high is None:
            decision.update({
                "status": "skipped",
                "activated": False,
                "reason_code": "no_candles_in_window",
                "reason": (
                    f"{ticker} {strike} {stype}: not activated because the "
                    f"contract had no 1-minute candles inside "
                    f"{config.STRATEGY_TRACKER_WINDOW_START}–"
                    f"{config.STRATEGY_TRACKER_WINDOW_END} IST."
                ),
            })
            return decision

        # ---- 4. Contract's own window high vs its own R2 --------- #
        assert contract_r2 is not None  # guaranteed by step 2
        if contract_window_high < contract_r2:
            decision.update({
                "status": "skipped",
                "activated": False,
                "reason_code": "contract_high_below_r2",
                "reason": (
                    f"{ticker} {strike} {stype}: not activated because the "
                    f"contract's own window high "
                    f"{contract_window_high:.2f} did not reach its own R2 "
                    f"{contract_r2:.2f} during "
                    f"{config.STRATEGY_TRACKER_WINDOW_START}–"
                    f"{config.STRATEGY_TRACKER_WINDOW_END} IST "
                    f"(gap {contract_gap_to_r2:+.2f} pts)."
                ),
            })
            return decision

        # ---- 5. Contract reached its own R2 → activate ----------- #
        did = self._activate(
            contract,
            index_key,
            contract_r2,
            contract_window_high,
            today,
            mode=mode,
            catchup=catchup,
            post_market=post_market,
            activation_method="catchup_contract_own_window_high",
            index_window_high=index_window_high,
            index_r2_value=index_r2_value,
        )

        decision.update({
            "status": "activated" if did else "skipped",
            "activated": bool(did),
            "reason_code": (
                "contract_reached_r2" if did else "activation_rejected"
            ),
            "reason": (
                f"{ticker} {strike} {stype}: "
                + (
                    f"activated because the contract's own window high "
                    f"{contract_window_high:.2f} reached its own R2 "
                    f"{contract_r2:.2f} during "
                    f"{config.STRATEGY_TRACKER_WINDOW_START}–"
                    f"{config.STRATEGY_TRACKER_WINDOW_END} IST "
                    f"(gap {contract_gap_to_r2:+.2f} pts)."
                    if did else
                    "not activated: the contract reached its own R2 but "
                    "the activation call rejected this contract."
                )
            ),
        })
        return decision

    @staticmethod
    def _ticker_and_name_for(index_key: str) -> tuple[str, str]:
        for spec in (config.STRATEGY_INDEXES or []):
            if not isinstance(spec, dict):
                continue
            if str(spec.get("instrument_key") or "").strip() == index_key:
                return (
                    str(spec.get("ticker") or "").strip() or "UNKNOWN",
                    str(spec.get("index_name") or "").strip() or "INDEX",
                )
        return (index_key, index_key)

    # ------------------------------------------------------------------ #
    # File writers                                                       #
    # ------------------------------------------------------------------ #
    def _write_decisions_file(
        self,
        *,
        today: str,
        mode: str,
        catchup: bool,
        post_market: bool,
        window_start_minute: str,
        window_end_minute: str,
        per_index_decisions: list[dict[str, Any]],
        activated_total: int,
        opening_minute: str,
    ) -> None:
        base = (
            Path(config.STRATEGY_BASE_DIR) / config.STRATEGY_STAGE_3_NAME
        )
        base.mkdir(parents=True, exist_ok=True)

        total_contracts = sum(
            int(d.get("contracts_total") or 0) for d in per_index_decisions
        )
        total_skipped = sum(
            int(d.get("contracts_skipped") or 0) for d in per_index_decisions
        )

        payload = {
            "status": "ok",
            "stage": "stage_3_decisions",
            "evaluation_mode": "per_contract",
            "window_rule": (
                f"only candles whose minute key satisfies "
                f"'{window_start_minute}' <= minute <= '{window_end_minute}'"
            ),
            "candle_isolation_rule": (
                "intraday fetch is sliced AT THE SOURCE; no downstream "
                "code ever sees an out-of-window candle"
            ),
            "day": today,
            "mode": mode,
            "catchup": catchup,
            "simulation": catchup,
            "post_market": post_market,
            "window_start": window_start_minute,
            "window_end": window_end_minute,
            "opening_minute": opening_minute,
            "generated_at": _now_iso(),
            "activated_total": activated_total,
            "contracts_total": total_contracts,
            "contracts_skipped": total_skipped,
            "indexes": per_index_decisions,
        }

        _write_json(base / "stage_3_decisions.json", payload)
        logger.info(
            "[stage_3] decisions file written — indexes=%d "
            "contracts_total=%d activated=%d skipped=%d window=[%s .. %s]",
            len(per_index_decisions),
            total_contracts,
            activated_total,
            total_skipped,
            window_start_minute,
            window_end_minute,
        )

    def _write_catchup_meta(
        self,
        *,
        mode: str,
        index_results: list[dict[str, Any]],
        post_market: bool,
        window_start_minute: str,
        window_end_minute: str,
        opening_minute: str,
    ) -> None:
        try:
            base = (
                Path(config.STRATEGY_BASE_DIR)
                / config.STRATEGY_STAGE_3_NAME
            )

            if post_market:
                timing_note = (
                    "The replay ran after the cash-market close, so the "
                    "1-minute candles used for each contract's window-high "
                    "comparison are the ones the market recorded between "
                    f"{window_start_minute[11:]} and "
                    f"{window_end_minute[11:]} IST — no post-window "
                    "candle was used."
                )
            else:
                timing_note = (
                    "The replay ran while the cash market was still open; "
                    "the 1-minute candles used for each contract's "
                    "window-high comparison are the ones recorded between "
                    f"{window_start_minute[11:]} and "
                    f"{window_end_minute[11:]} IST — no out-of-window "
                    "candle was used."
                )

            explanation = (
                f"Stage 3 was replayed in per-contract simulation mode. "
                f"Today's 1-minute intraday candles were fetched directly "
                f"from the Upstox REST API for every stage-2 contract, and "
                f"the response was sliced AT THE SOURCE to the strategy "
                f"window {window_start_minute[11:]}–"
                f"{window_end_minute[11:]} IST — no downstream code ever "
                f"sees an out-of-window candle. The contract's own "
                f"{opening_minute[11:]} opening-range candle defined its "
                f"own R2, and the contract's own window high across the "
                f"strategy window decided whether that R2 was reached. "
                f"The parent index is recorded as context only. "
                f"{timing_note}"
            )

            write_meta(
                base / "stage_3_meta_test.json",
                stage="stage_3",
                mode=mode,
                catchup=True,
                scheduled_time_ist=config.STRATEGY_TRACKER_WINDOW_END,
                explanation=explanation,
                payload={
                    "simulation": True,
                    "evaluation_mode": "per_contract",
                    "window_rule": (
                        f"only candles whose minute key satisfies "
                        f"'{window_start_minute}' <= minute <= "
                        f"'{window_end_minute}'"
                    ),
                    "candle_isolation_rule": (
                        "intraday fetch is sliced AT THE SOURCE; no "
                        "downstream code ever sees an out-of-window candle"
                    ),
                    "candles_source": "intraday_rest",
                    "post_market": post_market,
                    "cash_market_close": "15:30 IST",
                    "opening_minute": opening_minute,
                    "window_start": window_start_minute,
                    "window_end": window_end_minute,
                    "index_results": index_results,
                    "activated_count": len(self._activated),
                },
            )
        except Exception:
            logger.exception("[stage_3] meta write failed")

    # ------------------------------------------------------------------ #
    # Activation                                                         #
    # ------------------------------------------------------------------ #
    def _activate(
        self,
        contract: dict[str, Any],
        index_key: str,
        r2: float,
        touch_value: float,
        today: str,
        *,
        mode: str = "live",
        catchup: bool = False,
        post_market: bool = False,
        activation_method: str = "live",
        index_window_high: float | None = None,
        index_r2_value: float | None = None,
    ) -> bool:
        """
        Record a stage-3 activation.

        ``r2``         : the R2 level that was crossed.
        ``touch_value``: the value that crossed it (in live mode this is
                         the index LTP; in per-contract catch-up this is
                         the contract's own window high, computed only
                         from candles inside the strategy window).
        """
        key = str(contract.get("instrument_key") or "")
        if not key or key in self._activated:
            return False

        if catchup and post_market:
            explanation = (
                f"If the project had been running during the "
                f"{config.STRATEGY_TRACKER_WINDOW_START}–"
                f"{config.STRATEGY_TRACKER_WINDOW_END} IST window, this "
                f"contract would have been activated when its own window "
                f"high {touch_value:.2f} reached its own R2 {r2:.2f}. "
                f"The replay ran post-market and each contract's window "
                f"high was reconstructed from today's 1-minute intraday "
                f"candles fetched directly from the Upstox REST API, "
                f"sliced to the strategy window at fetch time."
            )
        elif catchup:
            explanation = (
                f"If the project had been running during the "
                f"{config.STRATEGY_TRACKER_WINDOW_START}–"
                f"{config.STRATEGY_TRACKER_WINDOW_END} IST window, this "
                f"contract would have been activated when its own window "
                f"high {touch_value:.2f} reached its own R2 {r2:.2f}. "
                f"Each contract's window high was reconstructed from "
                f"today's 1-minute intraday candles fetched directly "
                f"from the Upstox REST API, sliced to the strategy "
                f"window at fetch time."
            )
        else:
            explanation = (
                f"Live activation: {index_key} crossed R2 at {r2:.2f} "
                f"and triggered this contract."
            )

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
            "decision_basis": "per_contract" if catchup else "index_ltp",
            "r2_value": round(r2, 4),
            # Backwards-compatible field name; in per-contract catch-up
            # this carries the contract's own window high.
            "index_ltp_at_touch": round(touch_value, 4),
            "touch_value": round(touch_value, 4),
            "touch_source": (
                "contract_window_high"
                if activation_method == "catchup_contract_own_window_high"
                else "index_ltp"
            ),
            "direction": "above_r2",
            "touched_at": _now_iso(),
            "execution_mode": mode,
            "catchup": bool(catchup),
            "simulation": bool(catchup),
            "post_market": bool(post_market),
            "activation_method": activation_method,
            "explanation": explanation,
            # ---- parent index context -------------------------------- #
            "index_window_high": (
                round(index_window_high, 4)
                if index_window_high is not None else None
            ),
            "index_r2_value": (
                round(index_r2_value, 4)
                if index_r2_value is not None else None
            ),
        }

        if catchup:
            record["window_start"] = config.STRATEGY_TRACKER_WINDOW_START
            record["window_end"] = config.STRATEGY_TRACKER_WINDOW_END

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
            "[stage_3] activated %s (%s %s %s) — index=%s r2=%.2f "
            "touch=%.2f mode=%s catchup=%s post_market=%s method=%s",
            key, ticker, strike_price, strike_type, index_key,
            r2, touch_value, mode, catchup, post_market, activation_method,
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
            "evaluation_mode": "per_contract",
            "day": today,
            "generated_at": _now_iso(),
            "window_start": config.STRATEGY_TRACKER_WINDOW_START,
            "window_end": config.STRATEGY_TRACKER_WINDOW_END,
            "activated_count": len(self._activated),
            "activated": list(self._activated.values()),
        }
        _write_json(base / "stage_3_summary.json", summary)


strategy_live_tracker_service = StrategyLiveTrackerService()