# File: services/strategy_order_service.py
"""Stage 4 — selection + order placement.

At 09:18 IST (configurable), reads the stage-3 activations and:

  1. Keeps the N strikes per index nearest to that index's ATM strike
     (NIFTY: 3 by default, SENSEX: 5).
  2. Applies FIFO on R2-touch time across the combined pool.
  3. Places a paper order for the final pick and writes the order to::

         data/strategy/stage_4_order/<TICKER>/<INDEX_NAME>/orders.json

Candle discipline (IMPORTANT)
-----------------------------
This service never uses the whole-day intraday tape for anything.
The Upstox intraday endpoint returns the whole day's candles (there
is no query parameter for a time slice), so the response is sliced
**inside the fetch helper** to the strategy tracker window
(``STRATEGY_TRACKER_WINDOW_START`` → ``STRATEGY_TRACKER_WINDOW_END``,
default ``09:15 – 09:20``) the moment it arrives. Only that
pre-filtered list ever leaves the fetch helper — no downstream code
can see a candle outside the window.

When the run is a catch-up (the project started after the scheduled
09:18 IST time), the entry LTP is taken from the option's own
1-minute intraday candle whose minute key is **at or before** the
scheduled stage-4 minute, using the pre-filtered in-window list.
Because ``09:18`` lies inside ``[09:15, 09:20]``, the chosen candle
is guaranteed to be inside the strategy window. There is **no
fallback to live LTP** in catch-up mode — so the recorded entry
price always reflects the pipeline timing, not post-market noise.

In live (on-time) mode, the freshest live-feed LTP is used, which is
by construction the price at the scheduled minute.

Order meta
----------
Every order record carries:

    execution_mode     : startup | daily | manual | scheduled
    catchup            : bool (true when the run is a late catch-up)
    simulation         : bool (true when the run is a catch-up replay)
    post_market        : bool (true when the run happened after 15:30 IST)
    scheduled_time_ist : "09:18"
    actual_time_ist    : "HH:MM:SS"
    entry_ltp          : float | null — close of the option's
                         1-minute candle at or before the scheduled
                         minute (catch-up), or the freshest live LTP
                         (live).
    entry_ltp_source   : "scheduled_minute_close" | "live_ltp" | None
    entry_ltp_minute   : the actual minute key of the candle used
                         (catch-up only)
    entry_window_start : the strategy window's left edge (ISO minute)
    entry_window_end   : the strategy window's right edge (ISO minute)
    explanation        : human-readable "if the pipeline had run on
                         schedule, this instrument would have been
                         selected because …"

Diagnostics (added for the 2026-10-22 ``no_activations`` bug)
-------------------------------------------------------------
Stage 4 used to read ``stage_3_summary.json``, see an empty
``activated`` list, and report ``no_activations`` — with no log line
to say *why* the list was empty. On 2026-10-22 that message was the
only symptom the operator got, and it could mean four very different
things:

    * stage_3_summary.json was never written (Stage 3 never ran),
    * stage_3_summary.json was written by a previous day (stale file),
    * stage_3_summary.json was written today but with 0 activations
      (Stage 3 ran, no contract reached its own R2 — a quiet day),
    * stage_3_summary.json was written today, has activations, but
      every pick has entry_ltp = None because the picked contracts
      were never subscribed to the live feed.

This module now:

    * classifies the Stage-3 summary into one of four explicit states
      via ``_classify_stage3_summary()``: ``ok`` /
      ``empty_activations`` / ``stale_day`` / ``missing``;
    * logs the classified state (with ``day``, ``generated_at``,
      ``activated`` count, and any ``contracts_missing_level`` value
      propagated from Stage 3) at INFO before doing any work;
    * emits a tailored WARNING / INFO message in the
      ``no_activations`` branch per state, naming the next diagnostic
      endpoint (``/admin/strategy/tracker/status``,
      ``/admin/strategy/stage3``) or confirming a quiet day;
    * logs one INFO line per pick (``ticker strike type key
      entry_ltp source``) so the selected contract is visible without
      reading orders.json;
    * emits an aggregate WARNING in live mode when any placed order
      has ``entry_ltp = None`` — the second half of the 2026-10-22
      failure signature — with the checklist (``GET /live/status``)
      and a fix pointer to ``strategy_selection_service._register_strike``;
    * adds ``stage_3_summary_state`` / ``stage_3_summary_day`` /
      ``orders_with_ltp`` / ``orders_without_ltp`` to the
      ``stage_4_summary.json`` payload and to the returned dict.

Catch-up semantics
------------------
The unified refresh routine calls this service with ``catchup=True``
whenever the project starts (or daily / hard refresh fires) after the
scheduled 09:18 IST time and still inside the catch-up window
(09:15 – 23:59 IST).

Stage 3 (upstream) already restricts its decision to the 09:15–09:20
window, so the activations this service receives are the ones a live
run at 09:20 would have produced. Stage 4 then re-applies the
scheduled-time selection rules (nearest to ATM, FIFO by touch time),
so the resulting pick is exactly the one the pipeline would have made
at 09:18 — including the entry price.

The order is a JSON record — the real Upstox order API is not called.
A ``note`` field records this so downstream tooling can distinguish
paper from live orders.

The service never raises; failures are reported in the summary.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from core import config
from core.logger import get_logger
from services.live_feed_service import live_feed_service
from services.strategy_meta import (
    market_now,
    write_meta,
)
from services.token_service import token_service

logger = get_logger(__file__)

# Upstox REST endpoint used for the catch-up entry price.
_UPSTOX_BASE = "https://api.upstox.com"
_HTTP_TIMEOUT = 20

# Cash-market close in the market timezone. Anything after this is
# recorded as post-market in the order record / meta test file.
_MARKET_CLOSE_TIME = dtime(15, 30)


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


def _normalise_hhmm(text: str, default: str) -> str:
    """Return an ``HH:MM`` string from ``text`` (or the default)."""
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
    """True after the Indian cash-market close (15:30 IST)."""
    return _market_now().time() > _MARKET_CLOSE_TIME


def _strategy_window_bounds(today: str) -> tuple[str, str]:
    """Return the strategy tracker window as ISO minute keys on ``today``.

    These are the ONLY minute keys Stage 4 will accept from the
    intraday fetch. Same source of truth as Stage 3.
    """
    start = _normalise_hhmm(config.STRATEGY_TRACKER_WINDOW_START, "09:15")
    end = _normalise_hhmm(config.STRATEGY_TRACKER_WINDOW_END, "09:20")
    return f"{today}T{start}", f"{today}T{end}"


def _scheduled_stage_4_hhmm() -> str:
    return (
        f"{config.STRATEGY_ORDER_HOUR:02d}:"
        f"{config.STRATEGY_ORDER_MINUTE:02d}"
    )


def _scheduled_stage_4_minute_key(today: str) -> str:
    """Return the ISO minute key for the scheduled stage-4 time.

    Example: ``"2026-10-16T09:18"``. Minute keys are zero-padded ISO
    strings, so lexicographic comparison is valid.
    """
    return f"{today}T{_scheduled_stage_4_hhmm()}"


def _classify_stage3_summary(
    *,
    exists: bool,
    day: str,
    today: str,
    activated_count: int,
) -> str:
    """Return a short state string for the Stage-3 summary file.

    Values:

        "missing"            — the file does not exist. Stage 3 has not
                               run for today, or its write failed.
        "stale_day"          — the file exists but its ``day`` field
                               does not match today's date. Usually a
                               manual re-run against a previous day's
                               tree.
        "empty_activations"  — the file exists, day matches today, but
                               no contract reached its own R2. This is
                               a *normal* quiet-day state.
        "ok"                 — the file exists, day matches today, and
                               there is at least one activation.
    """
    if not exists:
        return "missing"
    if day and day != today:
        return "stale_day"
    if activated_count == 0:
        return "empty_activations"
    return "ok"


class StrategyOrderService:

    # ------------------------------------------------------------------ #
    # Public entry                                                       #
    # ------------------------------------------------------------------ #
    def run(
        self,
        reason: str = "manual",
        *,
        mode: str | None = None,
        catchup: bool = False,
    ) -> dict[str, Any]:
        """Run stage 4.

        Parameters
        ----------
        reason : trigger label ("startup" / "daily" / "manual" / "scheduled")
        mode   : alias for reason; kept separate so a caller can pass a
                 scheduled reason with a distinct mode.
        catchup: True when the run is a late catch-up (started after the
                 scheduled 09:18 IST time and inside the catch-up window).
        """
        effective_mode = str(mode or reason or "manual")
        executed_at = market_now()

        logger.info(
            "[stage_4:%s] starting (mode=%s catchup=%s post_market=%s "
            "scheduled=%s actual=%s)",
            reason,
            effective_mode,
            catchup,
            _post_market(),
            _scheduled_stage_4_hhmm(),
            _market_hhmmss(),
        )

        if not config.STRATEGY_ENABLED or not config.STRATEGY_ORDER_ENABLED:
            logger.info("[stage_4:%s] disabled by config", reason)
            # NOTE: scheduled_time_ist / actual_time_ist / day are
            # populated here too so the Telegram completion wrapper
            # does not print "Scheduled: None / Actual: None".
            return {
                "status": "disabled",
                "stage": "stage_4",
                "reason": reason,
                "mode": effective_mode,
                "catchup": catchup,
                "simulation": bool(catchup),
                "post_market": _post_market(),
                "scheduled_time_ist": _scheduled_stage_4_hhmm(),
                "actual_time_ist": _market_hhmmss(),
                "day": executed_at.date().isoformat(),
                "generated_at": _now_iso(),
                "placed": 0,
                "candidates": 0,
                "orders": [],
            }

        stage3_base = (
            Path(config.STRATEGY_BASE_DIR) / config.STRATEGY_STAGE_3_NAME
        )
        stage4_base = (
            Path(config.STRATEGY_BASE_DIR) / config.STRATEGY_STAGE_4_NAME
        )

        # ---- Pre-flight: classify the Stage-3 summary ---------------- #
        # Four states, each needing a different operator action:
        #   missing           → Stage 3 never ran; check tracker status
        #   stale_day         → summary is from a previous day
        #   empty_activations → normal quiet day (no action needed)
        #   ok                → proceed to pick
        stage3_summary_path = stage3_base / "stage_3_summary.json"
        stage3_summary_exists = stage3_summary_path.exists()
        stage3_summary = _read_json(stage3_summary_path) or {}
        stage3_day = str(stage3_summary.get("day") or "").strip()
        stage3_generated_at = str(
            stage3_summary.get("generated_at") or ""
        ).strip()
        today_iso = executed_at.date().isoformat()
        activations = stage3_summary.get("activated") or []

        stage3_state = _classify_stage3_summary(
            exists=stage3_summary_exists,
            day=stage3_day,
            today=today_iso,
            activated_count=len(activations),
        )

        logger.info(
            "[stage_4:%s] stage_3 summary: state=%s exists=%s day=%s "
            "generated_at=%s activated=%d contracts_missing_level=%s",
            reason,
            stage3_state,
            stage3_summary_exists,
            stage3_day or "?",
            stage3_generated_at or "?",
            len(activations),
            stage3_summary.get("contracts_missing_level", "?"),
        )

        # ---- No activations branch (diagnostics-aware) -------------- #
        if not activations:
            if stage3_state == "missing":
                logger.warning(
                    "[stage_4:%s] no stage-3 activations because "
                    "stage_3_summary.json does NOT exist at %s. "
                    "Stage 3 may not have run for today, or its write "
                    "failed. Check: GET /admin/strategy/tracker/status "
                    "and GET /admin/strategy/stage3.",
                    reason,
                    stage3_summary_path,
                )
            elif stage3_state == "stale_day":
                logger.warning(
                    "[stage_4:%s] no stage-3 activations today because "
                    "stage_3_summary.json is from %s (today is %s). "
                    "The file is stale — likely a manual re-run against "
                    "a previous day's tree, or a cron misfire. Check: "
                    "GET /admin/strategy/stage3.",
                    reason,
                    stage3_day or "?",
                    today_iso,
                )
            else:
                # stage3_state == "empty_activations"
                logger.info(
                    "[stage_4:%s] stage_3_summary.json exists for today "
                    "(%s, generated %s) but has 0 activations. This is "
                    "the normal quiet-day state — no contract's own "
                    "09:15–09:20 window high reached its own R2. "
                    "Per-contract reasons are in "
                    "data/strategy/stage_3_r2_activated/"
                    "stage_3_decisions.json.",
                    reason,
                    today_iso,
                    stage3_generated_at or "?",
                )
            # NOTE: scheduled_time_ist / actual_time_ist / day are
            # populated so the Telegram wrapper can display them.
            return {
                "status": "no_activations",
                "stage": "stage_4",
                "reason": reason,
                "mode": effective_mode,
                "catchup": catchup,
                "simulation": bool(catchup),
                "post_market": _post_market(),
                "scheduled_time_ist": _scheduled_stage_4_hhmm(),
                "actual_time_ist": _market_hhmmss(),
                "day": today_iso,
                "generated_at": _now_iso(),
                "placed": 0,
                "candidates": 0,
                "orders": [],
                # ---- new diagnostic fields -------------------------- #
                "stage_3_summary_state": stage3_state,
                "stage_3_summary_day": stage3_day or None,
            }

        # Extra warning when the summary is stale but still has
        # activations — this is a real risk of placing yesterday's
        # picks as today's orders. Not an error the code currently
        # guards against, but must be visible.
        if stage3_state == "stale_day":
            logger.warning(
                "[stage_4:%s] stage_3_summary.json is from %s (today is "
                "%s) but contains %d activation(s). These activations "
                "will be processed as today's picks — verify this is "
                "intended before wiring real order placement.",
                reason,
                stage3_day or "?",
                today_iso,
                len(activations),
            )

        today = today_iso

        # Resolve the token once for the whole stage-4 run so every
        # entry-price fetch reuses the same credential.
        try:
            token = token_service.get_access_token()
        except Exception:
            token = None
        if token is None:
            logger.info(
                "[stage_4:%s] no Upstox token — entry LTP will be "
                "unavailable in catch-up mode",
                reason,
            )

        # ---- 1. Split by ticker -----------------------------------------
        by_ticker: dict[str, list[dict[str, Any]]] = {}
        for a in activations:
            t = str(a.get("ticker") or "").upper().strip()
            if t:
                by_ticker.setdefault(t, []).append(a)

        # ---- 2. Nearest-to-ATM per index -------------------------------- #
        picks: list[dict[str, Any]] = []
        picks.extend(
            self._pick_nearest(
                by_ticker.get("NIFTY", []),
                config.STRATEGY_ORDER_NIFTY_PICKS,
            )
        )
        picks.extend(
            self._pick_nearest(
                by_ticker.get("SENSEX", []),
                config.STRATEGY_ORDER_SENSEX_PICKS,
            )
        )

        # ---- 3. FIFO by touched_at -------------------------------------- #
        picks.sort(key=lambda p: str(p.get("touched_at") or ""))
        final = picks[: config.STRATEGY_ORDER_FINAL_PICKS]

        logger.info(
            "[stage_4:%s] candidates=%d -> picks=%d -> final=%d "
            "(mode=%s catchup=%s post_market=%s)",
            reason,
            len(activations),
            len(picks),
            len(final),
            effective_mode,
            catchup,
            _post_market(),
        )

        # ---- 3b. Per-pick diagnostic line ------------------------------ #
        # One line per pick so the selection is visible without reading
        # orders.json. The entry LTP is filled in after _place_order,
        # so log the selection first, then the LTP health after the loop.
        for p in final:
            logger.info(
                "[stage_4:%s] pick: %s %s %s key=%s "
                "r2=%s touched_at=%s",
                reason,
                p.get("ticker"),
                p.get("strike_price"),
                p.get("strike_type"),
                p.get("upstox_instrument_key") or p.get("instrument_key"),
                p.get("r2_value"),
                p.get("touched_at"),
            )

        # ---- 4. Place paper order for each final pick ------------------- #
        orders: list[dict[str, Any]] = []
        for p in final:
            order = self._place_order(
                p,
                picks,
                mode=effective_mode,
                catchup=catchup,
                reason=reason,
                today=today,
                token=token,
            )
            orders.append(order)
            self._materialise_stage4(
                stage4_base,
                p,
                order,
                mode=effective_mode,
                catchup=catchup,
            )

        # ---- 4b. Entry-LTP health --------------------------------------- #
        orders_with_ltp = sum(
            1 for o in orders if o.get("entry_ltp") is not None
        )
        orders_without_ltp = len(orders) - orders_with_ltp

        for o in orders:
            logger.info(
                "[stage_4:%s] placed: %s %s %s entry_ltp=%s "
                "source=%s minute=%s",
                reason,
                o.get("ticker"),
                o.get("strike_price"),
                o.get("strike_type"),
                o.get("entry_ltp"),
                o.get("entry_ltp_source"),
                o.get("entry_ltp_minute"),
            )

        # Live mode: entry_ltp is sourced from live_feed_service.
        # A None here means the picked contract has no tick — either
        # not subscribed, or subscribed but not yet pushed. This is
        # the second half of the 2026-10-22 failure signature, and
        # must be visible in the log.
        if orders_without_ltp > 0 and not catchup:
            logger.warning(
                "[stage_4:%s] %d/%d placed order(s) have entry_ltp=None "
                "in live mode. The live feed has no tick for those "
                "contracts. Check GET /live/status (feed.instrument_count "
                "vs /live/instruments count). If the picked contracts "
                "are missing from the feed, they were never subscribed "
                "— see strategy_selection_service._register_strike.",
                reason,
                orders_without_ltp,
                len(orders),
            )
        # Catch-up mode: entry_ltp=None means the intraday fetch
        # returned no in-window candle for the contract. That is
        # expected behaviour (the service deliberately does not fall
        # back to a live price in catch-up mode). Just an INFO.
        elif orders_without_ltp > 0 and catchup:
            logger.info(
                "[stage_4:%s] %d/%d placed order(s) have entry_ltp=None "
                "in catch-up mode — no in-window intraday candle existed "
                "at or before %s IST for those contracts. This is the "
                "documented behaviour (no fallback to live LTP in "
                "catch-up mode).",
                reason,
                orders_without_ltp,
                len(orders),
                _scheduled_stage_4_hhmm(),
            )

        # ---- 5. Write summary ------------------------------------------- #
        summary = {
            "status": "ok",
            "stage": "stage_4",
            "reason": reason,
            "mode": effective_mode,
            "catchup": catchup,
            "simulation": bool(catchup),
            "post_market": _post_market(),
            "scheduled_time_ist": _scheduled_stage_4_hhmm(),
            "actual_time_ist": _market_hhmmss(),
            "day": today,
            "generated_at": _now_iso(),
            "candidates": len(activations),
            "picks": len(picks),
            "placed": len(orders),
            # ---- new diagnostic fields -------------------------------- #
            "stage_3_summary_state": stage3_state,
            "stage_3_summary_day": stage3_day or None,
            "orders_with_ltp": orders_with_ltp,
            "orders_without_ltp": orders_without_ltp,
            "orders": orders,
        }
        try:
            stage4_base.mkdir(parents=True, exist_ok=True)
            _write_json(stage4_base / "stage_4_summary.json", summary)
        except Exception:
            logger.exception("[stage_4:%s] summary write failed", reason)

        logger.info(
            "[stage_4:%s] complete — placed=%d with_ltp=%d without_ltp=%d "
            "(mode=%s catchup=%s post_market=%s stage_3_state=%s)",
            reason,
            len(orders),
            orders_with_ltp,
            orders_without_ltp,
            effective_mode,
            catchup,
            _post_market(),
            stage3_state,
        )
        return summary

    # ------------------------------------------------------------------ #
    # Selection helpers                                                  #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pick_nearest(
        pool: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        if not pool:
            return []

        def _dist(a: dict[str, Any]) -> float:
            try:
                sp = float(a.get("strike_price") or 0)
                atm = float(a.get("atm_strike") or sp)
                return abs(sp - atm)
            except (TypeError, ValueError):
                return float("inf")

        return sorted(pool, key=_dist)[: max(1, int(limit))]

    # ------------------------------------------------------------------ #
    # Intraday fetch — WINDOW-BOUNDED at the source                      #
    # ------------------------------------------------------------------ #
    def _fetch_intraday_candles(
        self,
        instrument_key: str,
        token: str | None,
        *,
        window_start_minute: str,
        window_end_minute: str,
    ) -> list[dict[str, Any]]:
        """Fetch today's 1-min intraday candles, sliced to the window.

        The Upstox endpoint returns the WHOLE day's tape — there is no
        query parameter for a time slice. This helper slices the
        response the moment it arrives, keeping ONLY candles whose
        ``minute`` key falls inside the inclusive range
        ``[window_start_minute, window_end_minute]``.

        Every downstream consumer of this list (entry-LTP lookup,
        audit trail, etc.) therefore sees only in-window candles. No
        code path can see an out-of-window candle.
        """
        key = str(instrument_key or "").strip()
        if not key or not token:
            return []

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
                "[stage_4] intraday fetch failed (network) for %s", key,
            )
            return []

        if resp.status_code >= 400:
            logger.info(
                "[stage_4] intraday fetch HTTP %s for %s: %s",
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

        # ---- Slice at the source: only in-window candles leave here --- #
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

    # ------------------------------------------------------------------ #
    # Entry-price lookup (catch-up aware, STRICTLY time-bounded)         #
    # ------------------------------------------------------------------ #
    def _entry_ltp(
        self,
        pick: dict[str, Any],
        *,
        today: str,
        catchup: bool,
        token: str | None,
    ) -> tuple[float | None, str | None, str | None]:
        """Return ``(ltp, source, minute_key)`` for the order entry.

        Rules
        -----
        * Live (non-catch-up) run:
              Use the freshest live-feed LTP. ``source="live_ltp"``,
              ``minute_key=None``.
        * Catch-up run:
              Fetch today's intraday tape for the contract, **already
              sliced at the source** to the strategy tracker window
              (``09:15 – 09:20``). Walk the pre-filtered list and take
              the last candle whose minute key is **at or before** the
              scheduled stage-4 minute (default ``09:18``). Because the
              list is already window-bounded and ``09:18`` lies inside
              the window, the chosen candle is guaranteed to be inside
              the strategy window.

              If no such candle exists, return ``(None, None, None)``.
              There is **no fallback to live LTP** in catch-up mode.
        """
        # ---- Live path: freshest available tick ----------------------
        if not catchup:
            live = self._live_ltp(pick)
            return live, ("live_ltp" if live is not None else None), None

        # ---- Catch-up path: strictly time-bounded candle close ------- #
        window_start_minute, window_end_minute = _strategy_window_bounds(today)
        target_minute = _scheduled_stage_4_minute_key(today)

        upstox_key = (
            pick.get("upstox_instrument_key")
            or pick.get("instrument_key")
        )
        if not upstox_key:
            logger.info(
                "[stage_4] no Upstox key for %s — entry LTP unavailable",
                pick.get("instrument_key"),
            )
            return None, None, None

        if not token:
            logger.info(
                "[stage_4] no token — cannot fetch intraday for %s",
                upstox_key,
            )
            return None, None, None

        candles = self._fetch_intraday_candles(
            str(upstox_key),
            token,
            window_start_minute=window_start_minute,
            window_end_minute=window_end_minute,
        )
        if not candles:
            logger.info(
                "[stage_4] no in-window intraday candles for %s "
                "(window %s .. %s) — entry LTP unavailable",
                upstox_key, window_start_minute, window_end_minute,
            )
            return None, None, None

        # Walk the already-window-bounded list forward and keep the
        # LAST candle whose minute key is <= the scheduled minute.
        # Minute keys are zero-padded ISO strings, so lexicographic
        # comparison is correct.
        best: dict[str, Any] | None = None
        for c in candles:
            minute = str(c.get("minute") or "")
            if not minute:
                continue
            if minute <= target_minute:
                best = c
            else:
                break

        if best is None:
            logger.info(
                "[stage_4] no in-window candle at/before %s for %s "
                "— entry LTP unavailable",
                target_minute, upstox_key,
            )
            return None, None, None

        try:
            close = float(best.get("close"))
        except (TypeError, ValueError):
            return None, None, None

        return close, "scheduled_minute_close", str(best.get("minute"))

    @staticmethod
    def _live_ltp(pick: dict[str, Any]) -> float | None:
        instrument_key = pick.get("instrument_key")
        ltp = None
        if pick.get("upstox_instrument_key"):
            ltp = live_feed_service.get_latest_ltp(
                pick.get("upstox_instrument_key")
            )
        if ltp is None and instrument_key:
            ltp = live_feed_service.get_latest_ltp(instrument_key)
        return ltp

    # ------------------------------------------------------------------ #
    # Order placement (paper)                                            #
    # ------------------------------------------------------------------ #
    def _place_order(
        self,
        pick: dict[str, Any],
        candidates: list[dict[str, Any]],
        *,
        mode: str,
        catchup: bool,
        reason: str,
        today: str,
        token: str | None,
    ) -> dict[str, Any]:
        instrument_key = pick.get("instrument_key")
        post_market = _post_market()

        ltp, ltp_source, ltp_minute = self._entry_ltp(
            pick,
            today=today,
            catchup=catchup,
            token=token,
        )

        window_start_minute, window_end_minute = _strategy_window_bounds(today)

        explanation = self._build_order_explanation(
            pick,
            mode=mode,
            catchup=catchup,
            ltp=ltp,
            ltp_source=ltp_source,
            ltp_minute=ltp_minute,
            post_market=post_market,
        )

        return {
            "day": pick.get("day"),
            "ticker": pick.get("ticker"),
            "index_name": pick.get("index_name"),
            "index_key": pick.get("index_key"),
            "instrument_key": instrument_key,
            "upstox_instrument_key": pick.get("upstox_instrument_key"),
            "strike_price": pick.get("strike_price"),
            "strike_type": pick.get("strike_type"),
            "side": "BUY",
            "quantity": config.STRATEGY_ORDER_QTY,
            "r2_value": pick.get("r2_value"),
            "index_ltp_at_touch": pick.get("index_ltp_at_touch"),
            "index_touched_at": pick.get("touched_at"),
            "selection_method": "nearest_to_atm_then_fifo",
            "selected_at": _now_iso(),
            "placed_at": _now_iso(),
            "entry_ltp": ltp,
            "entry_ltp_source": ltp_source,
            "entry_ltp_minute": ltp_minute,
            "entry_window_start": window_start_minute,
            "entry_window_end": window_end_minute,
            "status": "paper_filled",
            # ---- mandatory mode / catch-up meta ------------------------ #
            "execution_mode": mode,
            "catchup": bool(catchup),
            "simulation": bool(catchup),
            "post_market": bool(post_market),
            "trigger": str(reason or mode),
            "scheduled_time_ist": _scheduled_stage_4_hhmm(),
            "actual_time_ist": _market_hhmmss(),
            "explanation": explanation,
            "note": (
                "Paper order — real Upstox order API is not wired in this "
                "build. Wire /v2/order/place here to go live."
            ),
            "candidates_before_fifo": [
                {
                    "instrument_key": c.get("instrument_key"),
                    "strike_price": c.get("strike_price"),
                    "strike_type": c.get("strike_type"),
                    "touched_at": c.get("touched_at"),
                }
                for c in candidates
            ],
        }

    @staticmethod
    def _build_order_explanation(
        pick: dict[str, Any],
        *,
        mode: str,
        catchup: bool,
        ltp: float | None,
        ltp_source: str | None,
        ltp_minute: str | None,
        post_market: bool,
    ) -> str:
        ticker = pick.get("ticker") or "?"
        strike = pick.get("strike_price")
        stype = pick.get("strike_type")
        atm = pick.get("atm_strike")
        r2 = pick.get("r2_value")
        touched_at = pick.get("touched_at")

        window_start = _normalise_hhmm(
            config.STRATEGY_TRACKER_WINDOW_START, "09:15",
        )
        window_end = _normalise_hhmm(
            config.STRATEGY_TRACKER_WINDOW_END, "09:20",
        )

        # ---- Header: which flavour of execution was this? ------------- #
        if catchup and post_market:
            header = (
                f"Simulated post-market catch-up ({mode} mode). The "
                f"project started after the scheduled stage-4 time "
                f"({_scheduled_stage_4_hhmm()} IST) and after the "
                f"cash-market close; the pipeline was replayed at "
                f"{_market_hhmmss()} IST against the "
                f"{window_start}–{window_end} tracker window."
            )
        elif catchup:
            header = (
                f"Simulated intraday catch-up ({mode} mode). The project "
                f"started after the scheduled stage-4 time "
                f"({_scheduled_stage_4_hhmm()} IST); the pipeline was "
                f"replayed at {_market_hhmmss()} IST against the "
                f"{window_start}–{window_end} tracker window."
            )
        else:
            header = (
                f"On-time execution ({mode} mode) at "
                f"{_market_hhmmss()} IST."
            )

        # ---- Body: why this strike won -------------------------------- #
        body = (
            f" Selected {ticker} {strike} {stype} because it was the "
            f"strike closest to ATM ({atm}) among the stage-3 activated "
            f"contracts, and its parent index ({pick.get('index_key')}) "
            f"crossed R2 at {r2} at {touched_at}. FIFO ordering on "
            f"R2-touch time placed it first."
        )

        # ---- Entry LTP provenance ------------------------------------- #
        if ltp is not None and ltp_source == "scheduled_minute_close":
            body += (
                f" Entry LTP: {ltp} — close of the option's own "
                f"1-minute candle at or before "
                f"{_scheduled_stage_4_hhmm()} IST"
                + (f" (minute {ltp_minute})." if ltp_minute else ".")
                + f" The intraday fetch was sliced at the source to the "
                f"{window_start}–{window_end} IST strategy window, so no "
                f"candle outside that window could be used."
            )
        elif ltp is not None and ltp_source == "live_ltp":
            body += f" Entry LTP at execution time: {ltp}."
        else:
            body += (
                " Entry LTP is unavailable: no intraday candle existed "
                f"inside the {window_start}–{window_end} IST strategy "
                f"window at or before "
                f"{_scheduled_stage_4_hhmm()} IST for this contract, and "
                "the service intentionally does not fall back to a "
                "post-scheduled live price."
            )

        return header + body

    # ------------------------------------------------------------------ #
    # Stage-4 materialisation                                            #
    # ------------------------------------------------------------------ #
    def _materialise_stage4(
        self,
        stage4_base: Path,
        pick: dict[str, Any],
        order: dict[str, Any],
        *,
        mode: str,
        catchup: bool,
    ) -> None:
        ticker = str(pick.get("ticker") or "UNKNOWN")
        index_name = str(pick.get("index_name") or "INDEX")
        strike_type = str(pick.get("strike_type") or "")
        strike_price = pick.get("strike_price")
        post_market = bool(order.get("post_market"))

        idx_out = stage4_base / ticker / index_name
        idx_out.mkdir(parents=True, exist_ok=True)

        # One orders.json per index folder, appended.
        orders_path = idx_out / "orders.json"
        existing = _read_json(orders_path) or {
            "day": order.get("day"),
            "ticker": ticker,
            "index_name": index_name,
            "mode": mode,
            "catchup": catchup,
            "simulation": bool(catchup),
            "post_market": post_market,
            "orders": [],
        }
        existing["orders"].append(order)
        existing["updated_at"] = _now_iso()
        existing["mode"] = mode
        existing["catchup"] = catchup
        existing["simulation"] = bool(catchup)
        existing["post_market"] = post_market
        _write_json(orders_path, existing)

        # ---- Meta test file: human-readable audit trail ------------- #
        meta_path = idx_out / "order_meta_test.json"

        window_start = _normalise_hhmm(
            config.STRATEGY_TRACKER_WINDOW_START, "09:15",
        )
        window_end = _normalise_hhmm(
            config.STRATEGY_TRACKER_WINDOW_END, "09:20",
        )

        if catchup and post_market:
            audit_note = (
                "If the pipeline had run on schedule, this instrument "
                "would have been the stage-4 pick. This run was a "
                "post-market catch-up; Stage 3 was replayed against the "
                f"{window_start}–{window_end} window, and the entry LTP "
                "is the close of the option's 1-minute candle at or "
                f"before the scheduled {_scheduled_stage_4_hhmm()} IST "
                "minute — the candle was drawn from a tape already "
                "sliced to the strategy window at fetch time, so no "
                "post-window candle was ever considered."
            )
        elif catchup:
            audit_note = (
                "If the pipeline had run on schedule, this instrument "
                "would have been the stage-4 pick. This run was an "
                "intraday simulation: Stage 3 was replayed against the "
                f"{window_start}–{window_end} window, and the entry LTP "
                "is the close of the option's 1-minute candle at or "
                f"before the scheduled {_scheduled_stage_4_hhmm()} IST "
                "minute — the candle was drawn from a tape already "
                "sliced to the strategy window at fetch time."
            )
        else:
            audit_note = "Pipeline ran on schedule."

        write_meta(
            meta_path,
            stage="stage_4",
            mode=mode,
            catchup=catchup,
            scheduled_time_ist=_scheduled_stage_4_hhmm(),
            explanation=(
                order.get("explanation")
                or "Stage-4 order decision recorded for audit."
            ),
            payload={
                "ticker": ticker,
                "index_name": index_name,
                "simulation": bool(catchup),
                "post_market": post_market,
                "orders_written": 1,
                "order": {
                    "instrument_key": order.get("instrument_key"),
                    "upstox_instrument_key": order.get("upstox_instrument_key"),
                    "strike_price": order.get("strike_price"),
                    "strike_type": order.get("strike_type"),
                    "side": order.get("side"),
                    "quantity": order.get("quantity"),
                    "r2_value": order.get("r2_value"),
                    "index_ltp_at_touch": order.get("index_ltp_at_touch"),
                    "index_touched_at": order.get("index_touched_at"),
                    "entry_ltp": order.get("entry_ltp"),
                    "entry_ltp_source": order.get("entry_ltp_source"),
                    "entry_ltp_minute": order.get("entry_ltp_minute"),
                    "entry_window_start": order.get("entry_window_start"),
                    "entry_window_end": order.get("entry_window_end"),
                },
                "audit": {
                    "pipeline_stage_1": "09:16:30 IST",
                    "pipeline_stage_2": "09:17:00 IST",
                    "pipeline_stage_3_window": (
                        f"{window_start}–{window_end} IST"
                    ),
                    "pipeline_stage_4": _scheduled_stage_4_hhmm() + " IST",
                    "cash_market_close": "15:30 IST",
                    "candle_isolation_rule": (
                        "the intraday fetch is sliced AT THE SOURCE to "
                        f"{window_start}–{window_end} IST; no downstream "
                        "code ever sees an out-of-window candle"
                    ),
                    "entry_ltp_rule": (
                        "close of the option's own 1-minute candle at "
                        f"or before {_scheduled_stage_4_hhmm()} IST, "
                        "drawn from a tape pre-sliced to the strategy "
                        "window; no post-window candle is used"
                    ),
                    "note": audit_note,
                },
            },
        )

        # Copy the selected strike folder into stage 4 (unchanged).
        src = pick.get("stage_2_folder")
        if src:
            src_path = Path(src)
            dst = idx_out / f"{strike_price}_{strike_type}"
            try:
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src_path, dst)
                _write_json(dst / "order_meta.json", order)
            except Exception:
                logger.exception(
                    "[stage_4] failed copying %s -> %s", src_path, dst,
                )


strategy_order_service = StrategyOrderService()