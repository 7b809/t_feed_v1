# services/scheduler_service.py
"""Singleton APScheduler wrapper.

Jobs:
    * token_lifecycle_check         — interval
    * daily_refresh                 — cron, 08:30 IST Mon-Fri
    * first_candle_calc             — cron, 09:16:10 IST Mon-Fri
    * readiness_digest              — cron, 08:45 IST
    * preflight_recheck             — cron, 09:05 IST
    * level_isolation_recheck       — cron, every 5 min, 09:16–15:30 IST Mon-Fri
    * daily_backup                  — cron, 16:00 IST
    * live_sr_refresh_from_api      — interval, every N minutes (live-buffer only)
    * replay_seed_at_open           — cron, 09:16 IST Mon-Fri
                                      (NEW 2026-11-04: deferred first-seed for
                                       pre-window startups; idempotent)
    * dummy_order_tracker           — interval, every DUMMY_ORDER_TRACK_INTERVAL_SECONDS
                                      (live-tracks dummy orders; short-circuits
                                       until SR backfill is ready)
    * sandbox_eod_exit              — cron, 15:15 IST Mon-Fri
                                      (NEW 2026-11-04: sandbox-owned end-of-day
                                       exit — must fire before broker's ~15:15-15:25
                                       auto-square-off and before the dummy's
                                       15:35 accounting EOD_CLOSE; see Bug B note
                                       in services/sandbox_execution_service.py)
    * isolated_pair_sr_minute_tick  — cron, every minute at :10, Mon-Fri,
                                      09:00–15:59 registered, internally
                                      gated to 09:17:10 → 15:30:10 IST
                                      (NEW 2026-11-06: per-minute SR refresh
                                       for the isolated strike pair — reads
                                       LEVEL_FILTER_INDEXES + dynamically
                                       registered isolated/opposite
                                       instruments, recomputes pivots, and
                                       atomically rewrites
                                       isolation/{symbol}_live_sr.json so
                                       dashboards and downstream consumers
                                       always see fresh, complete files)

Deterministic-replay contract (2026-11-04)
------------------------------------------
Three invariants keep the post-isolation execution pipeline reproducible:

1. ``_dummy_order_tick`` (and **any** other tick — present or future —
   that invokes ``dummy_order_service``, including a potential
   ``_strategy2_recheck``) MUST consult ``sr_backfill_ready()`` before
   calling into the dummy-order service. This is the same latch
   ``main.py`` uses at startup. Firing before the SR backfill is ready
   creates orders against a partially-built series — the
   14:16-on-2026-10-29 class of bug.

2. ``_refresh_live_sr`` (the every-2-minute API refresh) MUST NOT
   mutate the replay series used by the startup catch-up. It updates
   only the live buffer (``update_replay=False``). The replay series
   is built once per day and is then frozen; ``DUMMY_ORDER_DRIFT_GUARD``
   then guarantees that any existing order's entry/stop/target is
   never recomputed from a later snapshot.

3. ``_seed_replay_series_at_open`` is the ONLY deferred path allowed
   to write the replay series (``update_replay=True``). It exists to
   cover the case where the process was up *before* 09:15 — feed-start
   backfill correctly skips (outside window) but nothing else would
   flip ``is_ready_for_today()`` for the rest of the day. The job is
   idempotent: it returns immediately when the replay is already
   seeded. Without it, a pre-09:15 startup silently disables
   ``dummy_order_tracker`` until the next restart.

Live sandbox execution — EOD ownership (2026-11-04, Bug B fix)
--------------------------------------------------------------
The sandbox executor (``services/sandbox_execution_service.py``) owns its
own end-of-day exit at ``SANDBOX_EOD_HOUR:SANDBOX_EOD_MINUTE`` (default
15:15 IST). This is deliberately EARLIER than two other events:

    * the broker's automatic intraday square-off (~15:15–15:25 IST),
    * the dummy order engine's accounting EOD close (15:35 IST default).

The ``sandbox_eod_exit`` job fires at 15:15 and calls
``sandbox_execution_service.force_eod_exit()``. Because the sandbox is
flat by 15:15, the later dummy ``EOD_CLOSE`` transition at 15:35 falls
into the "already exited" no-op branch inside
``sandbox_execution_service._handle_eod_close`` and is logged as the
*expected* divergence between the two engines (this is not a bug).

This job deliberately does NOT consult ``sr_backfill_ready()``.
Safety first: an un-exitable position is strictly worse than an exit
taken against a partially-built SR series. The service itself is a
no-op when no open position exists, so an accidental early fire is
harmless.

Isolated-pair SR per-minute refresh (2026-11-06)
------------------------------------------------
``isolated_pair_sr_minute_tick`` closes the "stale baseline" gap left
by the event-driven ``on_isolation_triggered`` / ``apply_promotion``
backfills (see ``isolated_instrument_promotion_service``) and the
per-export snapshots written by ``isolated_instrument_sr_export_service``.
Those write a baseline once at isolation/export time; between events,
levels on ``isolation/{symbol}_live_sr.json`` would otherwise sit at
their isolation-time state while the live buffer advances.

The tick:

    * fires every minute at ``:10`` seconds, Mon-Fri, cron
      ``hour="9-15"`` (window enforced *inside* the tick: 09:17:10
      through 15:30:10 IST inclusive);
    * collects its instrument set from
      ``config.LEVEL_FILTER_INDEXES`` **plus**
      ``live_sr_calc_service.active_sr_instruments`` (the set populated
      by ``isolated_instrument_promotion_service`` when an instrument
      qualifies for isolation);
    * for each key, calls
      ``live_sr_calc_service.calculate_pivots_cached(key, ..., force_refresh=True)``
      — which internally invokes
      ``upstox_client.HistoryV3Api.get_intra_day_candle_data(key, "minutes", "1")``,
      merges the historical tail, runs the strict TradingView pivot
      scan, filters to today's confirmed levels, and **updates the
      in-memory pivot cache** as a side-effect of the forced
      recompute;
    * atomically persists the resulting level set to
      ``<LIVE_DATA_DIR>/isolation/{safe_trading_symbol}_live_sr.json``
      via
      ``isolated_instrument_sr_export_service._atomic_write_json()``
      (``.tmp`` write → ``os.replace()``), so concurrent readers
      never see a half-written JSON.

The filename sanitization
(``IsolatedInstrumentSRExportService._safe_filename_for_sr``) is
deliberately identical to
``isolated_instrument_promotion_service._safe_filename``, so all three
writers (promotion backfill, export, minute tick) land on the **same**
file per instrument and simply overwrite each other — there is never
a stale or duplicated path.

Config flags:
    * ``ISOLATED_PAIR_SR_MINUTE_ENABLED`` (default ``True``) — register
      the job at all. Set to ``False`` to disable without code changes.
    * ``LIVE_SR_LEFT_BARS`` / ``LIVE_SR_RIGHT_BARS`` — the pivot window.

The job deliberately does NOT consult ``sr_backfill_ready()``:
this is a *refresh* of an already-established baseline, not a
first-order-creating tick. Adding the gate would freeze the baseline
on days where the SR backfill service is itself unhealthy — which is
exactly when operators most need the isolation files to stay
current.

See also:
    * ``services/sr_backfill_service.py::is_ready_for_today``  (the latch)
    * ``services/dummy_order_service.py``                      (consumer)
    * ``services/sandbox_execution_service.py``                (executor)
    * ``services/isolated_instrument_promotion_service.py``    (registration)
    * ``services/isolated_instrument_sr_export_service.py``    (atomic writer)
    * ``core/config.py::DUMMY_ORDER_*`` / ``SANDBOX_*``        (flags)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# DEFENSIVE CONTRACT — the single SR-backfill readiness latch                 #
# --------------------------------------------------------------------------- #
# Any tick that directly OR indirectly invokes ``dummy_order_service``       #
# MUST call ``sr_backfill_ready()`` first and bail on ``False``. This        #
# includes the in-file ``_dummy_order_tick`` below, and any out-of-file     #
# tick (e.g. a future ``_strategy2_recheck``) that invokes the service.     #
# The helper is intentionally module-level so external ticks can import    #
# it without going through ``scheduler_service``.                           #
# --------------------------------------------------------------------------- #
def sr_backfill_ready() -> tuple[bool, str]:
    """Gate dummy-order processing on fresh EMA V2 S/R for active isolation."""
    try:
        from services.level_isolated_instrument_service import level_isolated_instrument_service
        from services.external_sr_websocket_service import external_sr_websocket_service
        detail = level_isolated_instrument_service.get_detail() or {}
        isolated = detail.get("isolated") or {}
        key = str(isolated.get("instrument_key") or "").strip() if isinstance(isolated, dict) else ""
        if not key:
            return True, "no_active_isolation"
        if not external_sr_websocket_service.is_ready(key, "15/3"):
            return False, "external_sr_15_3_not_ready"
        return True, "external_sr_ready"
    except Exception as exc:
        logger.exception("[scheduler] external SR readiness probe failed")
        return False, f"external_sr_probe_error:{type(exc).__name__}"


def _resolve_label_to_index_key(label: str) -> str | None:
    """Resolve a ``LEVEL_FILTER_INDEXES`` label to its real Upstox key.

    Order of resolution:

      1. ``config.INDEX_LABEL_TO_KEY_MAP`` (operator override, if present).
      2. ``instrument_registry`` scan for a proper entry with
         ``underlying == label AND is_index == True``. Only entries
         whose ``instrument_key`` contains ``"|"`` are accepted —
         this deliberately skips the bogus ad-hoc entries that a
         previous buggy tick may have created (``instrument_key ==
         label``).
      3. ``_INDEX_LABEL_FALLBACK_MAP`` — hardcoded defaults for the
         common NSE/BSE indexes.

    Returns ``None`` when nothing resolves. Never raises.
    """
    label_upper = str(label or "").strip().upper()
    if not label_upper:
        return None

    # ---- 1. Operator override -------------------------------------- #
    try:
        override = getattr(config, "INDEX_LABEL_TO_KEY_MAP", None)
        if isinstance(override, dict):
            v = override.get(label_upper) or override.get(label)
            if v:
                return str(v)
    except Exception:
        logger.exception(
            "[scheduler] config.INDEX_LABEL_TO_KEY_MAP lookup raised"
        )

    # ---- 2. Registry scan (skip bogus ad-hoc entries) -------------- #
    try:
        from services.instrument_registry import instrument_registry
        for entry in instrument_registry.all():
            if not isinstance(entry, dict):
                continue
            if not entry.get("is_index"):
                continue
            u = str(entry.get("underlying") or "").strip().upper()
            k = str(entry.get("instrument_key") or "")
            # Require a real Upstox key shape ("SEGMENT|Name"). This
            # filters out the bogus ``BANKNIFTY`` ad-hoc entries a
            # previous buggy tick may have created.
            if u == label_upper and "|" in k:
                return k
    except Exception:
        logger.exception(
            "[scheduler] registry scan failed resolving label %r",
            label,
        )

    # ---- 3. Hardcoded fallback map --------------------------------- #
    fb = _INDEX_LABEL_FALLBACK_MAP.get(label_upper)
    if fb:
        return fb

    return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 string to a tz-aware datetime, or ``None``.

    Used by the freshness gate in ``_isolated_pair_sr_minute_tick`` to
    reject snapshots whose ``last_candle`` is not from today.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=_market_tz())
        except Exception:
            return None
    return dt


def _market_tz() -> ZoneInfo:
    """Return the configured market timezone (Asia/Kolkata fallback)."""
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _is_in_live_sr_window(now: datetime) -> bool:
    """Return True when ``now`` falls in 09:17:10 → 15:30:10 IST inclusive.

    Compares wall-clock (year/month/day/hour/minute/second) via a
    datetime comparison against the window endpoints on the same date.
    Never raises — returns False on any structural issue with ``now``.
    """
    try:
        start = now.replace(
            hour=_ISOLATED_SR_WINDOW_START_HOUR,
            minute=_ISOLATED_SR_WINDOW_START_MINUTE,
            second=_ISOLATED_SR_WINDOW_START_SECOND,
            microsecond=0,
        )
        end = now.replace(
            hour=_ISOLATED_SR_WINDOW_END_HOUR,
            minute=_ISOLATED_SR_WINDOW_END_MINUTE,
            second=_ISOLATED_SR_WINDOW_END_SECOND,
            microsecond=0,
        )
        return start <= now <= end
    except Exception:
        return False


def _get_active_sr_instrument_keys() -> list[str]:
    """Return the union of statically-configured and dynamically-tracked
    instrument keys, **resolved to real Upstox keys**.

    2026-11-06 fix: ``config.LEVEL_FILTER_INDEXES`` entries are folder
    labels (``"BANKNIFTY"``), not Upstox keys. They are now resolved via
    ``_resolve_label_to_index_key()`` before being returned. Items that
    already contain ``"|"`` are used directly. Items that cannot be
    resolved are logged and skipped — never passed through as raw
    labels (that was the previous bug that produced bogus
    ``isolation/BANKNIFTY_live_sr.json`` files with stale data).

    Also filters ``active_sr_instruments`` to entries containing ``"|"``
    so a stray label can never leak in from a downstream consumer.
    """
    keys: set[str] = set()

    # ---- 1. config.LEVEL_FILTER_INDEXES (resolve labels → keys) ---- #
    lfi = getattr(config, "LEVEL_FILTER_INDEXES", None)
    raw_items: list[str] = []
    if isinstance(lfi, str):
        raw_items = [s.strip() for s in lfi.split(",") if s.strip()]
    elif isinstance(lfi, (list, tuple, set, frozenset)):
        raw_items = [str(x).strip() for x in lfi if x]

    for item in raw_items:
        if not item:
            continue
        if "|" in item:
            # Already a real key — use as-is.
            keys.add(item)
            continue
        resolved = _resolve_label_to_index_key(item)
        if resolved:
            keys.add(resolved)
        else:
            logger.warning(
                "[scheduler] isolated_pair_sr_minute_tick: cannot resolve "
                "LEVEL_FILTER_INDEXES label %r to an Upstox key — "
                "skipping. Add it to config.INDEX_LABEL_TO_KEY_MAP or "
                "to _INDEX_LABEL_FALLBACK_MAP to include it.",
                item,
            )

    # ---- 2. dynamically-registered active_sr_instruments ----------- #
    try:
        from services.live_sr_calc_service import live_sr_calc_service
        active = getattr(live_sr_calc_service, "active_sr_instruments", None)
        if active is not None:
            for k in active:
                if not k:
                    continue
                sk = str(k).strip()
                if sk and "|" in sk:
                    keys.add(sk)
                elif sk:
                    logger.warning(
                        "[scheduler] active_sr_instruments contains a "
                        "non-key entry %r — skipping (expected "
                        "'SEGMENT|Name' shape)", sk,
                    )
    except Exception:
        logger.warning(
            "[scheduler] isolated_pair_sr_minute_tick: could not read "
            "live_sr_calc_service.active_sr_instruments",
            exc_info=True,
        )

    return sorted(keys)


def _trading_symbol_for_key(instrument_key: str) -> str:
    """Return the trading symbol for ``instrument_key``, or the key itself.

    Never raises. A failed registry lookup falls back to the raw key
    so the per-instrument file name is still deterministic.
    """
    try:
        from services.instrument_registry import instrument_registry
        entry = instrument_registry.get(instrument_key)
        if entry is None:
            entry = instrument_registry.register_adhoc(instrument_key)
        if entry:
            sym = entry.get("trading_symbol")
            if sym:
                return str(sym)
    except Exception:
        logger.debug(
            "[scheduler] isolated_pair_sr_minute_tick: trading_symbol "
            "lookup failed for %s",
            instrument_key,
            exc_info=True,
        )
    return str(instrument_key)


def _persist_live_sr_snapshot(
    instrument_key: str,
    result: dict,
    trading_symbol: str,
) -> str | None:
    """Atomically persist the SR snapshot to ``isolation/{symbol}_live_sr.json``.

    Uses ``IsolatedInstrumentSRExportService._atomic_write_json()``
    (tmp + ``os.replace()``) so readers never see a half-written JSON.
    The filename is produced by the same sanitizer the promotion and
    export services use, so all three writers target one canonical
    file per instrument.

    Never raises. Returns the written path on success, ``None`` on
    failure (logged).
    """
    try:
        from services.isolated_instrument_sr_export_service import (
            IsolatedInstrumentSRExportService,
        )
    except Exception:
        logger.exception(
            "[scheduler] isolated_pair_sr_minute_tick: cannot import "
            "IsolatedInstrumentSRExportService — skipping persist for %s",
            instrument_key,
        )
        return None

    try:
        levels = result.get("levels") or []
        params = result.get("params") or {}
        path = IsolatedInstrumentSRExportService._live_sr_path(
            trading_symbol
        )

        # Look up optional enrichment fields from the registry.
        underlying = None
        strike_price = None
        strike_type = None
        expiry = None
        try:
            from services.instrument_registry import instrument_registry
            entry = instrument_registry.get(instrument_key) or {}
            underlying = entry.get("underlying")
            strike_price = entry.get("strike_price")
            strike_type = entry.get("strike_type")
            expiry = entry.get("expiry")
        except Exception:
            pass

        payload = {
            "instrument_key": instrument_key,
            "trading_symbol": trading_symbol,
            "underlying": underlying,
            "strike_price": strike_price,
            "strike_type": strike_type,
            "expiry": expiry,
            "day": datetime.now(_market_tz()).date().isoformat(),
            "refreshed_at": datetime.now(_market_tz()).isoformat(),
            "minute_bucket": result.get("minute_bucket"),
            "generated_at": result.get("generated_at"),
            "params": {
                "left_bars": params.get("left_bars"),
                "right_bars": params.get("right_bars"),
                "only_today": params.get("only_today", True),
            },
            "candle_count": result.get("candle_count", 0),
            "first_candle": result.get("first_candle"),
            "last_candle": result.get("last_candle"),
            "level_count": len(levels),
            "counts": {
                "resistance": sum(
                    1 for l in levels if l.get("level") == "resistance"
                ),
                "support": sum(
                    1 for l in levels if l.get("level") == "support"
                ),
            },
            "levels": levels,
            "source": "scheduler_isolated_pair_sr_minute_tick",
        }

        IsolatedInstrumentSRExportService._atomic_write_json(path, payload)
        return str(path)
    except Exception:
        logger.exception(
            "[scheduler] isolated_pair_sr_minute_tick: persist failed for "
            "%s (symbol=%s)",
            instrument_key,
            trading_symbol,
        )
        return None


def _isolated_pair_sr_minute_tick() -> None:
    """Per-minute SR refresh for the isolated strike pair.

    Fires at :10 seconds each minute. The cron registration covers
    9:00–15:59 IST; the actual time window (09:17:10 → 15:30:10 IST
    inclusive) is enforced here so a misfire outside that window is a
    cheap no-op.

    For each key returned by ``_get_active_sr_instrument_keys()``:

      1. ``live_sr_calc_service.calculate_pivots_cached(force_refresh=True)``
         — recomputes from fresh intraday candles (HistoryV3Api),
         runs the strict pivot scan, filters to today, and updates
         the in-memory pivot cache.
      2. ``_persist_live_sr_snapshot()`` — atomically rewrites
         ``isolation/{symbol}_live_sr.json``.

    Never raises. Per-key errors are logged and the loop continues.
    """
    try:
        now = datetime.now(_market_tz())
    except Exception:
        logger.exception(
            "[scheduler] isolated_pair_sr_minute_tick: cannot resolve "
            "market timezone — skipping tick"
        )
        return

    if not _is_in_live_sr_window(now):
        # Cheap no-op outside the window — no API calls, no disk writes.
        return

    try:
        from services.live_sr_calc_service import live_sr_calc_service
    except Exception:
        logger.exception(
            "[scheduler] isolated_pair_sr_minute_tick: "
            "live_sr_calc_service import failed"
        )
        return

    left = max(1, min(100, int(getattr(config, "LIVE_SR_LEFT_BARS", 15))))
    right = max(1, min(100, int(getattr(config, "LIVE_SR_RIGHT_BARS", 15))))
    today = now.date()  # NEW — used by the freshness gate below

    keys = _get_active_sr_instrument_keys()
    if not keys:
        return

    ok_count = 0
    err_count = 0
    skipped_stale = 0
    for key in keys:
        try:
            result = live_sr_calc_service.calculate_pivots_cached(
                key,
                left_bars=left,
                right_bars=right,
                only_today=True,
                force_refresh=True,
            )
        except Exception:
            err_count += 1
            logger.exception(
                "[scheduler] isolated_pair_sr_minute_tick: "
                "calculate_pivots_cached raised for %s",
                key,
            )
            continue

        status = str(result.get("status") or "")
        if status != "ok":
            logger.debug(
                "[scheduler] isolated_pair_sr_minute_tick: %s status=%s "
                "err=%s",
                key, status, result.get("error"),
            )
            continue

        # ---- Freshness gate ------------------------------------- #
        # Only persist when the last candle is from TODAY. This
        # protects against three failure modes:
        #   1. Intraday fetch failed silently (invalid key, API
        #      hiccup) → merged = historical only → last_candle
        #      is yesterday or older.
        #   2. Pre-market startup (before 09:15) → no candles for
        #      today yet.
        #   3. Any future regression that hands us non-today data.
        # Re-running the tick cannot fix these — the file stays
        # whatever the last successful write was, which is correct.
        last_candle_str = result.get("last_candle") or ""
        last_dt = _parse_iso_datetime(last_candle_str)
        if last_dt is None or last_dt.date() != today:
            skipped_stale += 1
            logger.info(
                "[scheduler] isolated_pair_sr_minute_tick: %s "
                "last_candle=%s is not from today (%s) — "
                "skipping persist. Intraday fetch may have failed; "
                "check Upstox token and instrument_key=%r.",
                key, last_candle_str, today.isoformat(), key,
            )
            continue

        trading_symbol = _trading_symbol_for_key(key)
        written = _persist_live_sr_snapshot(key, result, trading_symbol)
        if written:
            ok_count += 1
        else:
            err_count += 1

    if ok_count or err_count or skipped_stale:
        logger.info(
            "[scheduler] isolated_pair_sr_minute_tick — "
            "keys=%d ok=%d err=%d stale_skipped=%d (left=%d, right=%d)",
            len(keys), ok_count, err_count, skipped_stale, left, right,
        )


class SchedulerService:
    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None

    @property
    def scheduler(self) -> BackgroundScheduler | None:
        return self._scheduler

    # ------------------------------------------------------------------ #
    # Public latch accessor — used by external ticks (e.g. a future      #
    # _strategy2_recheck) that need to honour the same SR-backfill      #
    # gate as the in-file dummy_order_tracker.                          #
    # ------------------------------------------------------------------ #
    def is_sr_backfill_ready(self) -> tuple[bool, str]:
        """Public accessor mirroring the module-level ``sr_backfill_ready``.

        Exposed on the singleton so out-of-file ticks can call
        ``scheduler_service.is_sr_backfill_ready()`` without importing
        the module-level helper directly.
        """
        return sr_backfill_ready()

    def start(self) -> BackgroundScheduler:
        from token_tasks.daily_backup_task import run_daily_backup
        from token_tasks.daily_refresh import run_daily_refresh
        from token_tasks.first_candle_task import run_first_candle_calculation
        from token_tasks.level_isolated_instrument_task import (
            run_level_isolation_recheck,
        )
        from token_tasks.readiness_digest_task import (
            run_preflight_recheck,
            run_readiness_digest,
        )
        from token_tasks.token_monitor import check_upstox_token_validity

        if self._scheduler is not None and self._scheduler.running:
            logger.info("Scheduler already running; skipping start.")
            return self._scheduler

        scheduler = BackgroundScheduler(timezone=config.MARKET_TIMEZONE)

        # -- token lifecycle -------------------------------------------- #
        scheduler.add_job(
            check_upstox_token_validity, "interval",
            minutes=config.UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES,
            id="token_lifecycle_check",
            replace_existing=True,
            kwargs={"send_success_message": False},
        )

        # -- daily refresh ---------------------------------------------- #
        if config.DAILY_REFRESH_ENABLED:
            scheduler.add_job(
                run_daily_refresh, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=config.DAILY_REFRESH_HOUR,
                minute=config.DAILY_REFRESH_MINUTE,
                id="daily_refresh",
                replace_existing=True, misfire_grace_time=300,
            )

        # -- first candle (09:16:10) ----------------------------------- #
        if getattr(config, "FIRST_CANDLE_ENABLED", True):
            scheduler.add_job(
                run_first_candle_calculation, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=getattr(config, "FIRST_CANDLE_HOUR", 9),
                minute=getattr(config, "FIRST_CANDLE_MINUTE", 17),
                second=getattr(config, "FIRST_CANDLE_SECOND", 10),
                id="first_candle_calc",
                replace_existing=True, misfire_grace_time=60,
            )

        # -- Readiness digest + pre-flight ------------------------------ #
        if config.READINESS_DIGEST_ENABLED:
            scheduler.add_job(
                run_readiness_digest, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=config.READINESS_DIGEST_HOUR,
                minute=config.READINESS_DIGEST_MINUTE,
                id="readiness_digest",
                replace_existing=True, misfire_grace_time=120,
            )

        if config.READINESS_PREFLIGHT_ENABLED:
            scheduler.add_job(
                run_preflight_recheck, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=config.READINESS_PREFLIGHT_HOUR,
                minute=config.READINESS_PREFLIGHT_MINUTE,
                id="preflight_recheck",
                replace_existing=True, misfire_grace_time=120,
            )

        # -- Level isolation recheck (every 5 min during market hours) -- #
        if getattr(config, "LEVEL_ISOLATION_ENABLED", True):
            scheduler.add_job(
                run_level_isolation_recheck, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour="9-15",
                minute="*/5",
                id="level_isolation_recheck",
                replace_existing=True, misfire_grace_time=120,
            )

        # -- Daily backup ----------------------------------------------- #
        if config.DAILY_BACKUP_ENABLED:
            scheduler.add_job(
                run_daily_backup, "cron",
                day_of_week=config.DAILY_BACKUP_DAYS,
                hour=config.DAILY_BACKUP_HOUR,
                minute=config.DAILY_BACKUP_MINUTE,
                id="daily_backup",
                replace_existing=True, misfire_grace_time=1800,
            )

        # -- Live SR refresh from Upstox API (LIVE BUFFER ONLY) -------- #
        # 2026-11-04 change: this tick MUST NOT touch the replay series
        # consumed by the startup catch-up. Otherwise
        # DUMMY_ORDER_DRIFT_GUARD would have to recompute entry/stop/
        # target for any pre-existing order whenever the snapshot_epoch
        # advanced — which is exactly the behaviour we removed. We now
        # pass ``update_replay=False``; the replay series is frozen
        # after the startup backfill and remains stable for the day.
        if getattr(config, "LIVE_SR_REFRESH_FROM_API_ENABLED", True):
            def _refresh_live_sr() -> None:
                try:
                    from services.sr_backfill_service import sr_backfill_service
                    try:
                        summary = sr_backfill_service.run(
                            reason="periodic_refresh",
                            send_telegram=False,
                            update_replay=_LIVE_SR_REFRESH_UPDATE_REPLAY,
                        )
                    except TypeError:
                        # The service has not yet been updated to accept
                        # ``update_replay``. Fall back to the legacy
                        # signature — which WILL reseed the replay
                        # series — and log loudly so the operator knows
                        # the safe path is not yet available.
                        logger.warning(
                            "[scheduler] sr_backfill_service.run() does not "
                            "accept update_replay=; falling back to the "
                            "legacy signature. The replay series WILL be "
                            "refreshed by this tick — DUMMY_ORDER_DRIFT_GUARD "
                            "must remain enabled until the service exposes "
                            "update_replay=False.",
                        )
                        summary = sr_backfill_service.run(
                            reason="periodic_refresh",
                            send_telegram=False,
                        )
                    logger.info(
                        "[scheduler] live_sr_refresh_from_api — "
                        "status=%s instruments=%s seeded=%s errors=%s "
                        "update_replay=%s",
                        summary.get("status"),
                        summary.get("instruments_total"),
                        summary.get("instruments_seeded"),
                        summary.get("errors"),
                        _LIVE_SR_REFRESH_UPDATE_REPLAY,
                    )
                except Exception:
                    logger.exception(
                        "[scheduler] live_sr_refresh_from_api failed"
                    )

            scheduler.add_job(
                _refresh_live_sr, "interval",
                minutes=max(1, int(getattr(
                    config, "LIVE_SR_REFRESH_FROM_API_MINUTES", 2,
                ))),
                id="live_sr_refresh_from_api",
                replace_existing=True, misfire_grace_time=60,
            )

        # -- Replay-series seed at market open (2026-11-04) ------------- #
        # Why this job exists
        # -------------------
        # Feed-start (live_feed_service._run_sr_backfill) is the primary
        # trigger for the first replay-series seed. It runs synchronously
        # inside live_feed_service.start(), which the startup pipeline
        # invokes BEFORE scheduler_service.start() — so on any normal
        # startup the replay is already seeded by the time
        # _dummy_order_tick is first eligible to fire.
        #
        # BUT: feed-start's backfill is gated on
        # sr_backfill_service.should_run(), which is window-gated to
        # [09:15, 23:59] IST on weekdays. If the process comes up
        # *before* 09:15 (e.g. 00:33 or 08:55), feed-start correctly
        # skips, and without this job NOTHING flips
        # is_ready_for_today() for the rest of the day —
        # dummy_order_tracker would skip every 30-second tick forever.
        #
        # This job is the missing retry:
        #   * Idempotent — returns immediately if is_ready_for_today()
        #     already True (normal startup path — feed-start already
        #     seeded).
        #   * update_replay=True — this is the ONLY deferred path
        #     allowed to touch the replay series. The periodic
        #     _refresh_live_sr tick keeps update_replay=False.
        #   * Window-gated by should_run() internally, so it never
        #     fires outside the trading window even if config drifts.
        #   * Runs one minute after open so the 09:15 candle is
        #     reliably available from the intraday API.
        if getattr(config, "SR_BACKFILL_ENABLED", True):
            def _seed_replay_series_at_open() -> None:
                try:
                    from services.sr_backfill_service import sr_backfill_service
                except Exception:
                    logger.exception(
                        "[scheduler] replay_seed_at_open: "
                        "sr_backfill_service import failed",
                    )
                    return

                # Idempotent fast-path: already seeded today.
                probe = getattr(
                    sr_backfill_service, "is_ready_for_today", None,
                )
                if probe is not None:
                    try:
                        if bool(probe()):
                            logger.info(
                                "[scheduler] replay_seed_at_open — "
                                "already seeded, skip",
                            )
                            return
                    except Exception:
                        logger.exception(
                            "[scheduler] replay_seed_at_open: "
                            "is_ready_for_today() probe raised; "
                            "proceeding to seed attempt",
                        )

                # Respect the trading-window gate (never fires outside it).
                should_run_fn = getattr(
                    sr_backfill_service, "should_run", None,
                )
                if should_run_fn is not None:
                    try:
                        should_run, skip_reason = should_run_fn()
                    except Exception:
                        logger.exception(
                            "[scheduler] replay_seed_at_open: "
                            "should_run() probe raised; proceeding to seed "
                            "attempt",
                        )
                        should_run, skip_reason = True, "probe_error"
                    if not should_run:
                        logger.info(
                            "[scheduler] replay_seed_at_open — "
                            "skipped: %s",
                            skip_reason,
                        )
                        return

                # Seed the replay series. This is the ONLY deferred path
                # allowed to pass update_replay=True.
                try:
                    summary = sr_backfill_service.run(
                        reason="market_open_backfill",
                        update_replay=True,
                    )
                    logger.info(
                        "[scheduler] replay_seed_at_open — "
                        "status=%s instruments=%s seeded=%s errors=%s "
                        "update_replay=True",
                        summary.get("status"),
                        summary.get("instruments_total"),
                        summary.get("instruments_seeded"),
                        summary.get("errors"),
                    )
                except Exception:
                    logger.exception(
                        "[scheduler] replay_seed_at_open failed; "
                        "dummy_order_tracker will continue to skip until "
                        "the seed succeeds (retry via POST /admin/hard-refresh "
                        "or wait for the next scheduled retry)",
                    )

            scheduler.add_job(
                _seed_replay_series_at_open, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=_REPLAY_SEED_HOUR,
                minute=_REPLAY_SEED_MINUTE,
                id="replay_seed_at_open",
                replace_existing=True,
                misfire_grace_time=_REPLAY_SEED_MISFIRE_GRACE_SECONDS,
            )

        # -- Isolated-pair SR per-minute refresh (2026-11-06) ---------- #
        # Every minute at :10 seconds, Mon-Fri. The cron hour="9-15"
        # covers 9:00–15:59; the tick itself gates the window to
        # 09:17:10 → 15:30:10 IST inclusive. Runs for every key in
        # config.LEVEL_FILTER_INDEXES plus every dynamically
        # registered isolated/opposite instrument in
        # live_sr_calc_service.active_sr_instruments.
        #
        # See the module docstring "Isolated-pair SR per-minute
        # refresh (2026-11-06)" section for the full rationale and
        # the cooperative contract with the promotion / export
        # services (same filename sanitizer, same atomic writer).
        if getattr(config, "ISOLATED_PAIR_SR_MINUTE_ENABLED", True):
            scheduler.add_job(
                _isolated_pair_sr_minute_tick, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour="9-15",
                minute="*",
                second=_ISOLATED_SR_CRON_SECOND,
                id="isolated_pair_sr_minute_tick",
                replace_existing=True,
                # 30s grace absorbs a brief scheduler pause (e.g. a slow
                # previous job finishing at :15) and still fires the
                # current minute's tick. Coalesce collapses any backlog
                # into one run; max_instances=1 prevents overlap if a
                # tick runs longer than 60s.
                misfire_grace_time=30,
                max_instances=1,
                coalesce=True,
            )

        # -- Dummy order live tracker ----------------------------------- #
        # Every DUMMY_ORDER_TRACK_INTERVAL_SECONDS (default 30 s) the
        # service polls the entry signal, creates any missing dummy
        # orders for armed candidates, updates open orders from live
        # LTP + periodic bar reconcile, and auto-closes at EOD.
        #
        # 2026-11-04: this tick now SHORT-CIRCUITS while the SR backfill
        # latch for today is not ready. The same latch is consulted by
        # main.py; here it prevents a mid-day restart (e.g. 14:16 on
        # 2026-10-29) from creating an order against a partially-built
        # SR series during the backfill window. It also gates against
        # the pre-09:15-startup gap covered by replay_seed_at_open
        # above — the tracker simply skips until that seed completes.
        #
        # The service is a no-op outside the DUMMY_ORDER window, so it
        # is safe to schedule all day.
        if getattr(config, "DUMMY_ORDER_ENABLED", True):
            def _dummy_order_tick() -> None:
                # -------- latch gate (MUST be first) ------------------ #
                ready, reason = sr_backfill_ready()
                if not ready:
                    logger.info(
                        "[scheduler] dummy_order_tracker skipped — %s",
                        reason,
                    )
                    return

                try:
                    from services.dummy_order_service import (
                        dummy_order_service,
                    )
                    summary = dummy_order_service.run_once(
                        reason="scheduled",
                    )
                    if summary.get("status") not in {"skipped"}:
                        logger.info(
                            "[scheduler] dummy_order_tracker — "
                            "status=%s created=%s updated=%s closed=%s "
                            "open=%s total=%s",
                            summary.get("status"),
                            summary.get("created"),
                            summary.get("updated"),
                            summary.get("closed"),
                            summary.get("open_count"),
                            summary.get("total_today"),
                        )
                except Exception:
                    logger.exception(
                        "[scheduler] dummy_order_tracker failed"
                    )

            scheduler.add_job(
                _dummy_order_tick, "interval",
                seconds=max(5, int(getattr(
                    config, "DUMMY_ORDER_TRACK_INTERVAL_SECONDS", 30,
                ))),
                id="dummy_order_tracker",
                replace_existing=True, misfire_grace_time=30,
            )

        # -- Sandbox EOD exit (Bug B fix, 2026-11-04) ------------------- #
        # The sandbox executor owns its own end-of-day exit at
        # SANDBOX_EOD_HOUR:SANDBOX_EOD_MINUTE (default 15:15 IST).
        #
        # Why this is a SEPARATE job from the dummy_order_tracker:
        #   * The dummy order engine's accounting EOD_CLOSE runs at
        #     DUMMY_ORDER_EOD_HOUR:DUMMY_ORDER_EOD_MINUTE (default
        #     15:35). That is TOO LATE for a real exit: NSE intraday
        #     F&O positions are broker-auto-squared-off around
        #     15:15–15:25, so by 15:35 the sandbox would already be
        #     flat and the executor's exit_all call would be a no-op
        #     or error.
        #   * Therefore the sandbox must own its exit at 15:15 — before
        #     both the broker's cut-off and the dummy's accounting tick.
        #   * When the dummy EOD_CLOSE transition fires at 15:35, the
        #     executor sees the position is already EXITED and logs
        #     the divergence as expected (see
        #     sandbox_execution_service._handle_eod_close).
        #
        # Why no sr_backfill_ready() gate here:
        #   * Safety first. An un-exitable position is strictly worse
        #     than an exit taken against a partially-built SR series.
        #     Blocking this job on the readiness latch would risk
        #     leaving the sandbox long into the broker's auto-square-off
        #     window. force_eod_exit() itself is idempotent and a no-op
        #     when no position is open, so an accidental early fire
        #     (e.g. operator restarts at 15:14) is harmless.
        #
        # Gating on SANDBOX_EXECUTION_ENABLED:
        #   * Default is false. On boxes that have not opted into live
        #     sandbox execution, no job is registered and the scheduler
        #     logs remain unchanged from pre-2026-11-04.
        if getattr(config, "SANDBOX_EXECUTION_ENABLED", False):
            _sandbox_eod_hour = int(getattr(config, "SANDBOX_EOD_HOUR", 15))
            _sandbox_eod_minute = int(getattr(config, "SANDBOX_EOD_MINUTE", 15))

            def _sandbox_eod_tick() -> None:
                try:
                    from services.sandbox_execution_service import (
                        sandbox_execution_service,
                    )
                    result = sandbox_execution_service.force_eod_exit()
                    logger.info(
                        "[scheduler] sandbox_eod_exit — status=%s open=%s",
                        (result or {}).get("status"),
                        (result or {}).get("open"),
                    )
                except Exception:
                    logger.exception(
                        "[scheduler] sandbox_eod_exit failed; the "
                        "executor's exit-all retries and the later "
                        "dummy 15:35 EOD_CLOSE no-op path will be "
                        "the fallback exit"
                    )

            scheduler.add_job(
                _sandbox_eod_tick, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=_sandbox_eod_hour,
                minute=_sandbox_eod_minute,
                id="sandbox_eod_exit",
                replace_existing=True,
                # Generous grace window: we would rather fire late
                # (still inside the broker auto-square-off window)
                # than not at all.
                misfire_grace_time=600,
            )

        # ------------------------------------------------------------------ #
        # DEFENSIVE CONTRACT — out-of-file ticks that invoke                 #
        # dummy_order_service (e.g. a future ``_strategy2_recheck``) MUST   #
        # call ``sr_backfill_ready()`` (module-level helper above) or the  #
        # singleton method ``scheduler_service.is_sr_backfill_ready()``    #
        # before calling into the service. Skipping this guard reopens the #
        # 14:16-on-2026-10-29 class of bug.                                #
        #                                                                   #
        # If you add a new job that indirectly reaches dummy_order_service,#
        # wrap it as:                                                       #
        #                                                                   #
        #     def _my_tick() -> None:                                       #
        #         ready, reason = sr_backfill_ready()                       #
        #         if not ready:                                             #
        #             logger.info("[scheduler] %s skipped — %s",            #
        #                         "_my_tick", reason)                       #
        #             return                                                #
        #         ...   # safe to invoke dummy_order_service here           #
        #                                                                   #
        # The corresponding test lives at tests/test_scheduler_service.py  #
        # (or equivalent) and asserts that no tick can reach                #
        # dummy_order_service when sr_backfill_ready() returns False.      #
        #                                                                   #
        # NOTE: the sandbox_eod_exit and isolated_pair_sr_minute_tick      #
        # jobs above are deliberately EXEMPT from this contract:           #
        #   * sandbox_eod_exit does not invoke dummy_order_service and     #
        #     safety requires it to fire regardless of SR readiness.       #
        #   * isolated_pair_sr_minute_tick is a per-minute refresh of     #
        #     an already-established baseline; freezing it on a stale     #
        #     SR-backfill latch is exactly the wrong failure mode when    #
        #     the backfill service is itself unhealthy.                   #
        # ------------------------------------------------------------------ #

        scheduler.start()
        self._scheduler = scheduler

        logger.info(
            "Scheduler started — daily=%02d:%02d, first_candle=%02d:%02d:%02d, "
            "digest=%02d:%02d, preflight=%02d:%02d, "
            "level_isolation_recheck=*/5 (9-15), DailyBackup=%02d:%02d, "
            "live_sr_refresh=%dm (enabled=%s, update_replay=%s), "
            "replay_seed_at_open=%02d:%02d (enabled=%s, gated_by=is_ready_for_today), "
            "isolated_pair_sr_minute_tick=*:*:%02d (enabled=%s, "
            "window=%02d:%02d:%02d-%02d:%02d:%02d), "
            "dummy_order_tracker=%ds (enabled=%s, gated_by=sr_backfill_ready), "
            "sandbox_eod_exit=%02d:%02d (enabled=%s, independent_of_dummy_eod)",
            config.DAILY_REFRESH_HOUR, config.DAILY_REFRESH_MINUTE,
            getattr(config, "FIRST_CANDLE_HOUR", 9),
            getattr(config, "FIRST_CANDLE_MINUTE", 16),
            getattr(config, "FIRST_CANDLE_SECOND", 10),
            config.READINESS_DIGEST_HOUR, config.READINESS_DIGEST_MINUTE,
            config.READINESS_PREFLIGHT_HOUR, config.READINESS_PREFLIGHT_MINUTE,
            config.DAILY_BACKUP_HOUR, config.DAILY_BACKUP_MINUTE,
            int(getattr(config, "LIVE_SR_REFRESH_FROM_API_MINUTES", 2)),
            bool(getattr(config, "LIVE_SR_REFRESH_FROM_API_ENABLED", True)),
            _LIVE_SR_REFRESH_UPDATE_REPLAY,
            _REPLAY_SEED_HOUR,
            _REPLAY_SEED_MINUTE,
            bool(getattr(config, "SR_BACKFILL_ENABLED", True)),
            _ISOLATED_SR_CRON_SECOND,
            bool(getattr(config, "ISOLATED_PAIR_SR_MINUTE_ENABLED", True)),
            _ISOLATED_SR_WINDOW_START_HOUR,
            _ISOLATED_SR_WINDOW_START_MINUTE,
            _ISOLATED_SR_WINDOW_START_SECOND,
            _ISOLATED_SR_WINDOW_END_HOUR,
            _ISOLATED_SR_WINDOW_END_MINUTE,
            _ISOLATED_SR_WINDOW_END_SECOND,
            int(getattr(config, "DUMMY_ORDER_TRACK_INTERVAL_SECONDS", 30)),
            bool(getattr(config, "DUMMY_ORDER_ENABLED", True)),
            int(getattr(config, "SANDBOX_EOD_HOUR", 15)),
            int(getattr(config, "SANDBOX_EOD_MINUTE", 15)),
            bool(getattr(config, "SANDBOX_EXECUTION_ENABLED", False)),
        )

        return scheduler

    def shutdown(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            try:
                job_count = len(self._scheduler.get_jobs())
            except Exception:
                job_count = -1
            try:
                self._scheduler.shutdown(wait=False)
                logger.info(
                    "Scheduler shutdown complete — %d job(s) stopped.",
                    job_count,
                )
            except Exception:
                logger.exception(
                    "Scheduler shutdown raised; forcing _scheduler = None."
                )
        self._scheduler = None
        logger.info(
            "Scheduler service is now stopped; a subsequent start() "
            "will create a new BackgroundScheduler."
        )


scheduler_service = SchedulerService()