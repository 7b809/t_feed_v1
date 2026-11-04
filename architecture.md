================================================================================
market-engine-v1 — Architecture, Flowchart & Maintenance Map
================================================================================
Purpose
-------
FastAPI market-data engine for:
  - Upstox token lifecycle via MongoDB + Telegram
  - Historical / intraday candle proxy
  - Backup extraction from MongoDB
  - Instrument registry from extracted backup tree
  - Upstox live WebSocket feed
  - Live 1-minute candle aggregation
  - Live EMA fast/slow cross detection
  - First Candle OR + level-instrument filter (per-index CE/PE strikes)
  - Level-isolation arbitration across four monitored indices
  - Live SR levels (pivot-based, per instrument)
  - Isolated-instrument SR tracker (pre/post-isolation snapshot)
  - Post-isolation deterministic dummy orders (t_feed_v1-market-engine-v3)
  - Scheduler for token checks, daily refresh, first-candle, isolation, SR refresh
  - Market holiday gate (Upstox Market Holidays & Timings API)
  - Runtime order toggle (enable / disable live Upstox orders)
  - Lot-based live order surface: place_order, exit_all_positions, margin_check
  - Admin + Telegram control of the above

IMPORTANT:
  README/architecture.md were outdated prior to earlier revisions, and are
  outdated AGAIN after the 2026-11-02 order-strategy rewrite and the
  2026-11-04 deterministic-replay guard additions. This document reflects
  the current code.

  Prior to 2026-11-02 the pipeline was:
      Strategy 1  (Stage 1: buffer filter -> Stage 2: isolation ->
                   Stage 3: R2 activation -> Stage 4: order)
      Strategy 2  (Stage 3: S3-touch watcher -> Stage 4: next-cross order)
  Strategy 1 / Stage 1-4 and Strategy 2 / Stage 3-4 names no longer
  appear in the current configuration or code. Their responsibilities
  have been refactored into the pipeline described below:
      first_candle_service          -> OR levels (per instrument)
      level_instrument_filter       -> CE/PE strike lists per index
      level_isolated_instrument     -> isolation on R3/S3 touch (arbitration)
      live_sr_levels_service        -> SR levels on the isolated instrument
      isolated_sr_tracker           -> pre/post-isolation SR snapshot
      dummy_order_service           -> t_feed_v1-market-engine-v3 execution

  Any operator who still has STRATEGY_STAGE_*, STRATEGY_2_STAGE_*, or
  the removed STRATEGY_EXT_* keys in .env is running on stale values —
  the current code ignores them (see the DEPRECATION sections in
  core/config.py and .env.example).

KEY ADDITIONS SINCE THE PREVIOUS REVISION
-----------------------------------------
A. 2026-11-02 — ORDER-STRATEGY REWRITE
   * services/dummy_order_service.py implements the
     t_feed_v1-market-engine-v3 spec (single-path post-isolation
     execution; the old Path A / Path B / Rupee-trailing /
     promotion-based counter-side order creation is REMOVED).
   * STRATEGY_EXT_* config keys are removed (except a deprecated
     STRATEGY_EXT_PATH_B_ENABLED shim that is ignored).
   * isolated_instrument_promotion_service.apply_promotion() is
     DORMANT — no code path calls it.
   * Removed: dummy_order_service Path A/B branching, regime-variant
     creation, MAX_ORDERS_PER_DAY cap, LTP+bar reconcile cadence.

B. 2026-11-04 — DETERMINISTIC REPLAY GUARDS
   * Six new DUMMY_ORDER_* flags make the post-isolation execution
     pipeline auditable and enforce "same day + same isolation record
     + same intraday-API response = same dummy orders, regardless of
     process start time":
         DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK            (default false)
         DUMMY_ORDER_STRICT_DETERMINISTIC_REPLAY         (default true)
         DUMMY_ORDER_WAIT_FOR_SR_BACKFILL                (default true)
         DUMMY_ORDER_WAIT_FOR_SR_BACKFILL_TIMEOUT_SECONDS (default 120)
         DUMMY_ORDER_DISABLE_RUNTIME_PATH_SWITCH         (default true)
         DUMMY_ORDER_DRIFT_GUARD                         (default true)
   * scheduler_service._dummy_order_tick() now short-circuits until
     sr_backfill_service.is_ready_for_today() returns True (the same
     latch main.py uses). Prevents the 30-second tick from creating
     orders against a partially-built series (the 14:16-on-2026-10-29
     bug class).
   * scheduler_service._refresh_live_sr() now passes
     update_replay=False. The every-2-minute refresh touches ONLY the
     live buffer; the replay series used by the startup catch-up is
     built once and then frozen for the day so DUMMY_ORDER_DRIFT_GUARD
     can guarantee existing entries are never recomputed.
   * services/config_validation.py gains:
         ERROR   if ALLOW_LTP_ENTRY_FALLBACK and STRICT_DETERMINISTIC_REPLAY
                 are both true (mutually exclusive in intent).
         WARNING if WAIT_FOR_SR_BACKFILL is false.
         WARNING if DISABLE_RUNTIME_PATH_SWITCH is false.
   * STRATEGY_ENTRY_* is now SIMULATION-ONLY. The diagnostic consumers
     (POST /admin/simulation/run, GET /admin/level-isolation/strategy-signal)
     still read those keys, but services/dummy_order_service.py does not.

C. Level-isolation pipeline (baseline, replacing Strategy 1/2)
   * services/first_candle_service.py            OR levels
   * services/level_instrument_filter_service.py CE/PE strikes per index
   * services/level_isolated_instrument_service.py isolation (arbitration)
   * services/live_sr_levels_service.py          pivot-based live SR
   * services/isolated_sr_tracker_service.py     pre/post-isolation SR
   * services/sr_backfill_service.py             intraday seed + replay
   * services/live_sr_calc_service.py            stateless SR calculator
   * services/dummy_order_service.py             t_feed_v1 execution

D. Market holiday service + gate + admin routes (baseline)
   * services/market_holiday_service.py
   * routes/market_holiday_routes.py
   * main.py::_holiday_gate_for_startup()

E. Runtime order toggle + order/margin HTTP surface + Telegram commands
   * services/order_runtime_service.py
   * upstox_services/_sanitise.py
   * upstox_services/lot_sizes.py
   * upstox_services/place_order.py
   * upstox_services/exit_all_positions.py
   * upstox_services/margin_check.py
   * routes/order_routes.py
   * routes/margin_routes.py
   * token_tasks/order_toggle_commands.py
   * token_tasks/telegram_token_bot.py

F. Security policy
   * No access token or account identifier appears in any log line
     or API response. Provider error bodies and verbose responses
     are redacted by upstox_services._sanitise.sanitise_payload.


================================================================================
1. RUNTIME ENTRY POINTS
================================================================================

Primary entry:
  main.py
  app = FastAPI(title="Market Data Foundation", lifespan=app_lifespan)

Launch commands:
  Procfile:   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
  run.bat:    python -m uvicorn main:app --host 0.0.0.0 --port 8010
  start.sh:   python -m uvicorn main:app --host 0.0.0.0 --port 8010
  update_project.py: git reset/clean/pull + start.sh/stop.sh

Lifespan:
  startup:
    _holiday_gate_for_startup()
      if is_holiday: skip unified refresh
                     start scheduler + telegram bot
                     send "APPLICATION STARTED · HOLIDAY"
      else:
        asyncio.to_thread(_run_full_refresh_sync, "startup", loop)
        Telegram startup notification
  shutdown:
    Telegram stopping notification
    live_feed_service.stop()
    strategy_2_live_tracker_service.stop()    # NOTE: retained for compat;
                                              # see §9 for current pipeline
    telegram_token_bot.stop()
    scheduler_service.shutdown()
    Telegram stopped notification


================================================================================
2. HIGH-LEVEL COMPONENT MAP
================================================================================

                         +-----------------------------+
                         |        Browser / UI         |
                         |  index, charts, logs pages  |
                         |  isolated-instrument page   |
                         +--------------+--------------+
                                        |
                                        | HTTP / WebSocket
                                        v
+--------------------------------------------------------------------------------+
|                              FastAPI main.py                                   |
|                                                                                |
|  Routers:                                                                      |
|    page_routes.py              /, /charts, /chart/{key}, /favicon.ico          |
|    live_routes.py              /live/*, /ws/live, /ws/chart, /all-feeds,       |
|                                /ws/ema-crosses, /ws/ema                        |
|    admin_routes.py             /admin/hard-refresh, /admin/backup-status,      |
|                                /admin/cleanup-status, /admin/cleanup           |
|    folder_routes.py            /admin/folders, /admin/folders/{name}/download  |
|    isolated_instrument_routes.py /isolated, /admin/level-isolation/*           |
|    live_sr_calc_routes.py      /live-sr/calc/*                                 |
|    dashboard_routes.py         /dashboard, /ui/dashboard/strategy              |
|    upstox_routes.py            /api/upstox/*                                   |
|    log_routes.py               /logs, /ui/logs, /ui/logs/{filename}            |
|    market_holiday_routes.py    /admin/market-holiday*                          |
|    order_routes.py             /admin/orders/*                                 |
|    margin_routes.py            /admin/orders/margin*                           |
+------------------------------------+-------------------------------------------+
                                     |
                                     v
+--------------------------------------------------------------------------------+
|                                  Services                                      |
|                                                                                |
|  Token / Telegram:                                                             |
|    services/token_service.py              cached Upstox token                  |
|    token_tasks/token_store.py             MongoDB token document               |
|    token_tasks/token_monitor.py           token validity checks                |
|    token_tasks/upstox_token_client.py     Upstox profile/funds validation      |
|    services/telegram_service.py           notifications                        |
|    token_tasks/telegram_token_bot.py      /save_token, /token_status, /orders* |
|    token_tasks/order_toggle_commands.py   /orders_enable, /orders_disable,     |
|                                           /orders_status                      |
|                                                                                |
|  Data / Registry:                                                              |
|    services/backup_data_service.py        Mongo blob -> data/ zip extraction   |
|    services/instrument_registry.py        scan data/ for ema_crosses.json      |
|    services/cleanup_service.py            delete transient folders             |
|    services/market_holiday_service.py     Upstox holiday check + cache         |
|    services/order_runtime_service.py      runtime order toggle (ON at start)   |
|    services/backfill_guard.py             ran_today / mark_ran meta_data guard |
|                                                                                |
|  Live pipeline:                                                                |
|    services/live_feed_service.py          Upstox WebSocket                     |
|    services/live_ema_engine.py            1-min candles + EMA cross detection  |
|    services/live_cross_store.py           live_crosses.json persistence        |
|    services/live_broadcast.py             in-process pub/sub                   |
|    services/live_backfill_service.py      seed EMA from backup + intraday      |
|                                                                                |
|  First-candle OR + level filter:                                               |
|    services/first_candle_service.py       OR levels + first_candle.json        |
|    services/level_instrument_filter_service.py  CE/PE strike list per index    |
|                                                                                |
|  Level isolation + SR:                                                         |
|    services/live_sr_levels_service.py     pivot-based live SR (per instrument)  |
|    services/sr_backfill_service.py        intraday seed + replay series        |
|    services/level_isolated_instrument_service.py  isolation + replay backfill  |
|    services/isolated_sr_tracker_service.py pre/post-isolation SR snapshot      |
|    services/live_sr_calc_service.py       stateless on-demand SR calculator    |
|                                                                                |
|  Post-isolation execution (t_feed_v1-market-engine-v3):                        |
|    services/dummy_order_service.py        deterministic dummy-order placement  |
|                                                                                |
|  Diagnostics:                                                                  |
|    services/strategy_entry_signal_service.py  SIMULATION-ONLY diagnostic view  |
|    services/simulation_service.py             POST /admin/simulation/run       |
|                                                                                |
|  Order surface (live Upstox orders):                                           |
|    upstox_services/_sanitise.py           shared redaction                     |
|    upstox_services/lot_sizes.py           NIFTY 65, SENSEX 20, …               |
|    upstox_services/place_order.py         OrderApiV3 MARKET buy/sell           |
|    upstox_services/exit_all_positions.py  OrderApi.exit_positions()            |
|    upstox_services/margin_check.py        ChargeApi.post_margin                |
|                                                                                |
|  Daily backup:                                                                 |
|    services/daily_backup_service.py       zip folders -> MongoDB               |
|                                                                                |
|  Scheduling:                                                                   |
|    services/scheduler_service.py          APScheduler singleton                |
+------------------------------------+-------------------------------------------+
                                     |
                                     v
+--------------------------------------------------------------------------------+
|                              External Systems                                  |
|                                                                                |
|  MongoDB:                                                                      |
|    MONGO_DB.TOKENS_COLLECTION          Upstox token document                   |
|    MONGO_DB.BACKUP_METADATA_COLLECTION candle-backup metadata                  |
|    MONGO_DB.BACKUP_BLOB_COLLECTION     candle-backup archive blob              |
|    DAILY_BACKUP_MONGO_DB.<coll>        daily data backup                       |
|                                                                                |
|  Upstox:                                                                       |
|    REST: profile, search, historical, intraday                                 |
|    REST: MarketHolidaysAndTimingsApi.get_holiday                               |
|    REST: OrderApiV3.place_order, OrderApi.exit_positions                       |
|    REST: ChargeApi.post_margin                                                 |
|    WebSocket: MarketDataStreamerV3                                             |
|                                                                                |
|  Telegram:                                                                     |
|    notifications + token management bot + order toggle commands                |
+--------------------------------------------------------------------------------+


================================================================================
3. STARTUP / UNIFIED REFRESH FLOW
================================================================================

All three refresh triggers use the SAME routine:
  - Project startup      -> main.app_lifespan -> _run_full_refresh_sync("startup")
  - Daily refresh        -> token_tasks/daily_refresh.py -> _run_full_refresh_sync("daily")
  - Manual hard refresh  -> POST /admin/hard-refresh -> _run_full_refresh_sync("manual")

Holiday gate (before the unified refresh):
  app_lifespan -> _holiday_gate_for_startup()
    +-- market_holiday_service.get_today()
    +-- if is_holiday:
    |     logger: "Holiday gate: skipping unified refresh"
    |     scheduler_service.start()
    |     telegram_token_bot.start()
    |     Telegram "APPLICATION STARTED · HOLIDAY"
    |     yield   (no refresh, no feed, no pipeline)
    +-- if status="error" (provider down):
          treat as NOT a holiday; continue with the full pipeline
    +-- else:
          continue with the full pipeline below

Flowchart:

uvicorn main:app
  |
  v
app_lifespan
  |
  v
_holiday_gate_for_startup()
  |
  +-- is_holiday? skip pipeline
  |
  v
asyncio.to_thread(_run_full_refresh_sync, reason, loop)
  |
  +-- 1. Telegram: "<Reason> Refresh Started"
  |
  +-- 2. cleanup_service.run(reason)
  |       removes CLEANUP_REMOVE_FOLDERS inside project root
  |       preserves CLEANUP_RESERVED_FOLDERS
  |
  +-- 3. token_service.refresh_tokens()
  |       reads MongoDB token doc into memory
  |
  +-- 4. check_upstox_token_validity(send_success_message=False)
  |       validates token via Upstox profile API
  |       updates token doc validation metadata
  |
  +-- 5. backup_data_service.refresh()
  |       reads BACKUP_BLOB_DOC_ID from MongoDB
  |       if binary data exists: extract zip into LIVE_DATA_DIR=data/
  |       if blob missing: log warning + file_link
  |
  +-- 6. instrument_registry.load()
  |       scans data/ recursively for ema_crosses.json
  |       builds _by_key, _by_triple, _ordered
  |
  +-- 6a. _ensure_core_indexes_registered()
  |        pre-registers NIFTY / BANKNIFTY / MIDCPNIFTY / SENSEX
  |        (DEFAULT_CORE_INSTRUMENTS) as ad-hoc entries
  |
  +-- 6b. BACKFILL: daily_refresh marker (only on startup)             <-- NEW
  |        if reason == "startup":
  |          should, why = _should_backfill_daily_refresh()
  |          if should: backfill_guard.mark_ran("daily_refresh", ...)
  |          else:     log "skipped — <why>"
  |
  +-- 7. Wire loop + cross handler
  |       live_broadcast.set_loop(loop)
  |       live_feed_service.set_loop(loop)
  |       _wire_cross_handler()
  |         on_cross(entry, cross):
  |           live_cross_store.save(entry, cross)
  |           live_broadcast.publish_sync("crosses", entry, {"cross": cross})
  |
  +-- 8. live_feed_service.start() or restart()
  |
  +-- 8a. BACKFILL: first_candle (only on startup)                     <-- NEW
  |        if reason == "startup":
  |          should, why = _should_backfill_first_candle()
  |          if should: first_candle_service.run(reason="startup_backfill",
  |                        backfill=True)
  |          else:     log "skipped — <why>"
  |        Extended window: 09:16 -> 23:59:59 IST on any weekday
  |        trading day. `backfill=True` allows post-close runs; the
  |        intraday API still serves today's 1-min candles after 15:30.
  |        This is what makes /isolated work when the project starts at
  |        e.g. 19:08 IST.
  |
  +-- 8b. Level-instrument filter + SR registration (only on startup)  <-- NEW
  |        level_instrument_filter_service.run(reason="startup")
  |        Reads first_candle.json, writes level_instruments.json
  |        (per-index CE/PE strike list) and registers the filtered
  |        strikes with live_sr_levels_service.
  |
  +-- 8c. LEVEL ISOLATION backfill (only on startup)                   <-- NEW
  |        level_isolated_instrument_service.run_backfill(reason="startup")
  |        Replays intraday candles from 09:15 -> now, re-applies the
  |        touch-arbitration rules, reconstructs the "would-have-
  |        isolated" instrument. Uses
  |        LEVEL_ISOLATION_REPLAY_USE_SERIES_OR and
  |        LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL to guard against
  |        phantom touches.
  |
  +-- 9. scheduler_service.start()
  |
  +-- 10. telegram_token_bot.start()
  |
  +-- 11. Telegram: "<Reason> Refresh Completed / Finished"
  |        Includes cleanup / token / backup / registry / first-candle /
  |        level-filter / level-isolation lines.
  |
  +-- 12. return structured result (includes market_snapshot,
           backfill_daily_refresh, first_candle, level_filter,
           level_isolation)
  |
  v
main logs "Application startup complete"
  |
  v
app yields (running)
  |
  v
shutdown:
  Telegram stopping
  live_feed_service.stop()
  telegram_token_bot.stop()
  scheduler_service.shutdown()
  Telegram stopped


================================================================================
4. LIVE DATA PIPELINE
================================================================================

(Unchanged from the previous revision. Reproduced here for completeness.)

+---------------------+
| Upstox WebSocket    |
| MarketDataStreamerV3|
+----------+----------+
           |
           | on("message", on_message)
           v
+----------------------------------+
| services/live_feed_service.py    |
| LiveFeedService._handle_feed()   |
+----------------------------------+
           |
           | for each instrument_key in payload["feeds"]
           v
+----------------------------------+
| registry lookup / ad-hoc register|
+----------------------------------+
           |
           v
+----------------------------------+
| _extract_tick(feed)              |
| Extracts: ltp, prev_close,       |
| volume, candle_1m = interval "I1"|
+----------------------------------+
           |
           +-------------------------------+
           |                               |
           v                               v
+------------------------+      +-------------------------------+
| Health feed logging    |      | live_ema_engine.on_tick()     |
+------------------------+      +---------------+---------------+ 
                                                |
                                                v
                                     +-------------------------------+
                                     | live_broadcast.publish_sync   |
                                     | topic "feed"                  |
                                     +-------------------------------+

EMA ENGINE INTERNAL FLOW: (unchanged)
  on_tick -> _ingest -> _normalise -> minute_key
    -> finalise previous forming -> append to rolling _candles
    -> _detect_cross -> _compute_cross -> on_cross handler
    -> live_cross_store.save + live_broadcast.publish_sync("crosses")

Consumers of the live candle series (beyond the EMA engine):
  * live_sr_levels_service  — pivot scan on every finalized candle,
                              persists live_sr_levels.json per instrument.
  * level_isolated_instrument_service — subscribes to index ticks to
                              fire R3/S3 touch checks (2 s throttle).
  * isolated_sr_tracker_service — hooks live_sr_levels_service's
                              _append_level to snapshot new pivots on
                              the isolated instrument only.


================================================================================
5. WEBSOCKET / BROADCAST ARCHITECTURE
================================================================================

(Unchanged from the previous revision. Reproduced here for completeness.)

Topics:
  "feed"    -> live tick snapshots
  "crosses" -> EMA cross records

WebSocket routes in routes/live_routes.py:
  /ws/live
  /ws/chart
  /all-feeds
  /ws/all-feeds
  /ws/ema-crosses
  /ws/ema


================================================================================
6. BACKUP EXTRACTION + INSTRUMENT REGISTRY
================================================================================

(Unchanged from the previous revision. Reproduced here for completeness.)

MongoDB candle-backup documents:
  BACKUP_METADATA_COLLECTION = app_tokens
  BACKUP_METADATA_DOC_ID     = backup_candles
  BACKUP_BLOB_COLLECTION     = backup_collection
  BACKUP_BLOB_DOC_ID         = backup_historical_candles

backup_data_service.refresh()
  -> read blob doc
  -> no blob doc:   status no_blob_doc
  -> blob missing:  status blob_missing
  -> extract into LIVE_DATA_DIR=data/
  -> return summary

instrument_registry.load()
  -> rglob("ema_crosses.json")
  -> build _by_key, _by_triple, _ordered


================================================================================
7. TOKEN LIFECYCLE
================================================================================

Components:
  services/token_service.py            cached Upstox token
  token_tasks/token_store.py           MongoDB token CRUD
  token_tasks/token_monitor.py         validity checks (PII masked)
  token_tasks/upstox_token_client.py   profile/funds validation
  token_tasks/telegram_token_bot.py    /save_token, /token_status,
                                       /token_check, /cancel, /help
                                       AND /orders_status, /orders_enable,
                                       /orders_disable

Token flow: unchanged.

Telegram order-toggle commands:
  /orders_status   -> services.order_runtime_service.status()
  /orders_enable   -> order_runtime_service.enable(source="telegram")
  /orders_disable  -> order_runtime_service.disable(source="telegram")

  These commands are dispatched by
  token_tasks/order_toggle_commands.handle_order_toggle_command()
  from telegram_token_bot.handle_message(). The dispatch block sits
  BEFORE the pending-token check so the commands work even while a
  token save is in progress and never get misrouted into the token
  entry flow.


================================================================================
8. FIRST CANDLE (OR) FLOW
================================================================================

(Replaces the former OPENING RANGE flow. opening_range_service has
been superseded by first_candle_service. The rest-fallback logic was
carried forward unchanged.)

Trigger:
  - scheduler job "first_candle_calc" at FIRST_CANDLE_HOUR:MINUTE:SECOND
    default 09:17:10 IST
  - startup / daily refresh / hard refresh also call
    first_candle_service.run(reason=..., backfill=True|False)

Flow:

first_candle_service.run(reason, backfill=False)
  |
  v
for each instrument_registry.all():
  _calculate_one(entry, target_minute)
    |
    v
  live_ema_engine.get_candles(key, limit=0)
    |
    v
  find candle where minute == "YYYY-MM-DDT09:15"
    |
    +-- missing:
    |     if FIRST_CANDLE_REST_FALLBACK_ENABLED:
    |       _fetch_first_candle_via_rest(key, target_minute)
    |       if found: use it; candle_source = rest_intraday_fallback
    |     else:
    |       skipped: first_candle_not_available
    |
    v
  compute_levels(high, low)
    H, L, A, D_H, D_L, R1, S1, R2, S2, R3, S3,
    R3_Threshold, S3_Threshold
    |
    v
  write first_candle.json next to ema_crosses.json
    or data/_adhoc/<safe_key>/first_candle.json
    persist candle_source, date, and OHLC
    |
    v
  return status ok/skipped/error

Backfill policy (2026-11-04 addition):
  On startup, admin_routes._should_backfill_first_candle() allows the
  job to run at ANY time between 09:17:00 and 23:59:59 IST on a
  weekday trading day if today's first_candle.json is missing, dated
  for an older day, or unparseable. The job is called with
  backfill=True so it ignores the normal session window. This makes
  /isolated work when the project is started post-close.

REST fallback policy (unchanged):
  - Timeout: FIRST_CANDLE_REST_TIMEOUT_SECONDS (default 15).
  - Uses the existing Upstox access token via token_service.
  - Never logs or returns the token.
  - Only fires when the in-memory EMA engine does not yet have the
    09:15 candle, so the live-feed path is unchanged on fast days.


================================================================================
9. ISOLATION + EXECUTION PIPELINE
================================================================================

Replaces the former Strategy 1 (Stage 1-4) and Strategy 2 (Stage 3-4)
pipelines. The 2026-11-02 order-strategy rewrite removed Path A / Path
B branching, Rupee trailing, promotion-based counter-side creation,
and the MAX_ORDERS_PER_DAY cap. The deterministic t_feed_v1 pipeline
below is now the single order-placement path.

Stage A — First candle OR + level filter (see §8 for OR)
─────────────────────────────────────────────────────────
  first_candle_service.run()          -> first_candle.json per instrument
  level_instrument_filter_service.run()
    -> reads data/index/<underlying>/first_candle.json
    -> emits level_instruments.json containing the CE/PE strikes that
       map to R1/R2/R3/R3_Threshold and S1/S2/S3/S3_Threshold.
    -> registers every filtered strike with live_sr_levels_service
       so SR tracking starts immediately.

Stage B — Level isolation (multi-index arbitration)
───────────────────────────────────────────────────
  level_isolated_instrument_service
    - watches STRATEGY_MONITORED_INDICES for R3 / S3 touches
      (arbitration order in STRATEGY_TOUCH_PRIORITY_ORDER).
    - on the FIRST winning touch of the day: fixes an isolation
      record at T_iso, capturing
        * trigger_index / trigger_side / trigger_level / trigger_ltp
        * T_iso / trigger_time
        * bias-side instrument (NEAREST NIFTY ROUND-50 STRIKE to
          NIFTY spot LTP; CE for R3, PE for S3)
        * a snapshot of the bias-side instrument's OWN price context
          (LTP, touch-minute OHLC, own OR levels, own first-candle OHLC).
    - persisted to data/level_isolated_instrument.json.
    - run_backfill(reason="startup") replays intraday candles to
      reconstruct a would-have-isolated instrument when the project
      starts mid-day.

Stage C — Isolated SR tracker
─────────────────────────────
  isolated_sr_tracker_service
    - snapshots live_sr_levels_service's current levels as
      pre_isolation_levels at T_iso.
    - thereafter records every NEW pivot confirmation on the SAME
      instrument as post_isolation_levels.
    - persists to data/level_isolated_instrument_sr.json.
    - dedup key: (level_type, price, confirmed_time).

Stage D — Deterministic dummy-order placement
─────────────────────────────────────────────
  services/dummy_order_service.run_once(reason="scheduled")
    The scheduler fires this every DUMMY_ORDER_TRACK_INTERVAL_SECONDS.
    On each tick, the service:

      1. Short-circuits when
         sr_backfill_service.is_ready_for_today() is False (the same
         latch main.py and the scheduler use).
      2. Reads the isolation record (bias-side instrument + T_iso).
      3. Waits for the FIRST 15/3 SR level on the bias side with
         pivot_time > T_iso AND confirmed_time > T_iso.
      4. Resolves the routing and the traded instrument's OWN OR grid
         (first_candle.json) using STRATEGY_ORDER_* tunables:
             Same leg:      Support -> bias side
                            Resistance -> counter side
             09:30 reversal: on or before 09:30:00 IST when a pre-T_iso
                            support was already confirmed and the
                            current level is a resistance -> SAME leg
      5. Regime check on the traded instrument's OWN OR grid:
             P_entry > R2                  -> SKIP
             P_entry < S2                  -> OR Low / +50 pt gain
             S2 <= P_entry < R1            -> R2, R3_Threshold, R3
             R1 <= P_entry <= R2 (Zone 2)  -> R3_Threshold, R3
      6. Stop-loss = P_entry * (1 - STRATEGY_ORDER_STOP_LOSS_PCT)  [30 %]
      7. Stage 1: milestone touch OR >= STRATEGY_ORDER_MILESTONE_GAIN_POINTS
      8. Stage 2: exit at the NEXT 15/3 resistance confirmation close
      9. Auto-close any surviving OPEN order at
         DUMMY_ORDER_EOD_HOUR:MINUTE (default 15:35).

    Deterministic-replay guards (2026-11-04):
        ALLOW_LTP_ENTRY_FALLBACK         (false)  debug-only escape hatch
        STRICT_DETERMINISTIC_REPLAY      (true)   skip unresolved entries
        WAIT_FOR_SR_BACKFILL             (true)   gate on backfill
        WAIT_FOR_SR_BACKFILL_TIMEOUT_SECONDS (120) cap on the gate
        DISABLE_RUNTIME_PATH_SWITCH      (true)   Path A/B from record only
        DRIFT_GUARD                      (true)   never overwrite stored
                                                  entry on catch-up

    Persisted to data/dummy_orders.json. Each order fires exactly TWO
    Telegram messages (creation, close). SKIPPED records fire a single
    WARNING.

Stage E — Simulation diagnostics (NOT order placement)
─────────────────────────────────────────────────────
  services/strategy_entry_signal_service.compute()
  services/simulation_service.py
    These read STRATEGY_ENTRY_* and produce a replay of the two legacy
    "signal_v2" and "regime" scenarios for POST /admin/simulation/run
    and GET /admin/level-isolation/strategy-signal. They have NO effect
    on services/dummy_order_service.py.

  The promotion service
  (isolated_instrument_promotion_service.apply_promotion) remains
  DORMANT — no code path calls it after the 2026-11-02 rewrite. The
  two config keys (STRATEGY_ENTRY_ALLOW_PROMOTION,
  STRATEGY_ENTRY_ALLOW_RE_PROMOTION) are kept for backward-compat.


================================================================================
10. MARKET HOLIDAY GATE
================================================================================

Service:
  services/market_holiday_service.py
    market_holiday_service.get_holiday(date_str=None)
    market_holiday_service.is_trading_holiday(date_str=None)
    market_holiday_service.get_today()
    market_holiday_service.clear_cache()
    market_holiday_service.last_status()

  Wraps Upstox MarketHolidaysAndTimingsApi.get_holiday.
  Normalises datetime (_date) and epoch-millis (start_time / end_time)
  fields to ISO strings. Per-date in-process cache with 1-hour TTL.
  Never raises; always returns a payload with status in
  {ok, holiday, not_holiday, error}.

Routes:
  GET  /admin/market-holiday
  GET  /admin/market-holiday/{date_str}
  GET  /admin/market-holiday/cache-status
  POST /admin/market-holiday/clear-cache

Startup gate:
  main._holiday_gate_for_startup()
    - called before _run_full_refresh_sync
    - returns {date, status, is_holiday, should_run,
               holiday_count, description, error, cached}
    - should_run = not is_holiday
      (provider errors -> should_run=True so a network outage
       never silently skips a real trading day)
    - on a holiday:
        scheduler_service.start()
        telegram_token_bot.start()
        Telegram "APPLICATION STARTED · HOLIDAY"
        pipeline skipped
    - the startup result now carries holiday_gate

  Also used by admin_routes._is_trading_holiday_today() to gate the
  daily_refresh and first_candle startup backfills.

Note:
  Reuse the gate in scheduled tasks (daily refresh, first-candle,
  isolation) if you want them to skip holidays too:
      if market_holiday_service.is_trading_holiday():
          logger.info("<task>: market holiday — skipping")
          return


================================================================================
11. ORDER SURFACE
================================================================================

Runtime toggle:
  services/order_runtime_service.py
    order_runtime_service.is_enabled()
    order_runtime_service.enable(source="api")
    order_runtime_service.disable(source="telegram")
    order_runtime_service.set(enabled, source)
    order_runtime_service.status()
      -> {enabled, changed_at, changed_by, default_on_startup}

  Default: ENABLED at process start. Thread-safe. Resets to ON on
  restart by design so a crash or deploy never leaves the engine
  silently unable to trade.

Dummy orders (t_feed_v1-market-engine-v3, see §9 Stage D):
  services/dummy_order_service.py
    The ONLY post-isolation order-placement path.
    Reads:
      - isolation record:  data/level_isolated_instrument.json
      - replay series:     built once at startup by
                           sr_backfill_service (frozen for the day;
                           LIVE_SR_REFRESH_FROM_API_* only touches
                           the live buffer via update_replay=False).
      - own OR grid:       data/<...>/first_candle.json
      - tunables:          core/config.py STRATEGY_ORDER_* block
      - guards:            core/config.py DUMMY_ORDER_* block
    Gated by order_runtime_service.is_enabled().
    Every record is labelled "dummy". No real Upstox order is ever
    placed by this service.

Lot sizes:
  upstox_services/lot_sizes.py
    LOT_SIZES = {
      "NIFTY": 65, "SENSEX": 20, "BANKNIFTY": 30,
      "FINNIFTY": 60, "MIDCPNIFTY": 120,
    }
    DEFAULT_LOT_SIZES_BY_EXCHANGE = {"NSE_FO": 65, "BSE_FO": 20}

    lots_to_quantity(underlying, lots, instrument_key)
      resolution order:
        1. explicit underlying
        2. registry underlying
        3. known-ticker substring scan of the key
        4. per-exchange default (NSE_FO -> 65, BSE_FO -> 20)
        5. unit fallback (qty = lots)
      returns (quantity, lot_size, source)
      source in {lot_table, lot_table_inferred, exchange_default,
                 unit_fallback}

Place order (live):
  upstox_services/place_order.py
    place_order(
        instrument_key,          # required
        lots=1,
        transaction_type="SELL",
        product="D",
        order_type="MARKET",
        price=0.0,
        trigger_price=0.0,
        validity="DAY",
        tag="",
        is_amo=False,
        slice=False,
    ) -> dict

    Uses OrderApiV3 + PlaceOrderV3Request.
    Gated on order_runtime_service.is_enabled().
    Never raises.
    Token read from services.token_service.
    Token never logged or returned; Upstox error bodies redacted.

Exit all positions (live):
  upstox_services/exit_all_positions.py
    exit_all_positions(dry_run=False) -> dict
    Wraps OrderApi.exit_positions().
    Gated on order_runtime_service.is_enabled().
    CLI: python -m upstox_services.exit_all_positions [--dry-run]

Margin check (live):
  upstox_services/margin_check.py
    check_margin(
        instrument_key,
        lots=1,
        transaction_type="BUY",
        product="D",
        price=0.0,
        verbose=False,
    ) -> dict

    Wraps ChargeApi.post_margin.
    verbose=False returns:
        status, instrument_key, underlying, transaction_type,
        lots, lot_size, lot_source, quantity, product,
        required_margin, final_margin, error, called_at
    verbose=True additionally carries a sanitised "response" dict.
    CLI: python -m upstox_services.margin_check
         --instrument-key "NSE_FO|47162"
         [--lots N] [--type BUY|SELL] [--product D|I]
         [--price X] [--verbose]

Shared sanitiser:
  upstox_services/_sanitise.py
    sanitise_payload(value)         recursive redaction
    redact_long_runs(text)          20+ character runs

    Applied to every Upstox response and every ApiException body
    preview before it reaches a log line or a return dict.

HTTP routes:
  routes/order_routes.py
    POST /admin/orders/buy
    POST /admin/orders/sell
    POST /admin/orders/exit-all
    GET  /admin/orders/runtime-status
    POST /admin/orders/runtime-enable
    POST /admin/orders/runtime-disable
    buy/sell require instrument_key (HTTP 400 otherwise).
    All three order endpoints pass through order_runtime_service.

  routes/margin_routes.py
    POST /admin/orders/margin         (single instrument)
    POST /admin/orders/margin/batch   (list of instruments)
    Body accepts instrument_key (required), lots (default 1),
    transaction_type (default BUY), product (default D),
    price (default 0.0), verbose (default False).
    Missing instrument_key -> HTTP 400.
    Provider / token errors -> HTTP 200 with status in
    {no_token, invalid_input, error}.

Telegram commands:
  /orders_status   current enable/disable state
  /orders_enable   turn orders ON
  /orders_disable  turn orders OFF
  Dispatched by token_tasks/order_toggle_commands.py from
  telegram_token_bot.handle_message() BEFORE the pending-token check.

Security policy:
  - No access token appears in any log line or returned dict.
  - No token_masked field is returned.
  - No Authorization header snippet is logged.
  - Upstox error bodies and verbose responses are redacted by
    upstox_services._sanitise.sanitise_payload before leaving the
    process.
  - A late-firing 401 / 403 from Upstox cannot leak a credential
    through a log file or an HTTP response.


================================================================================
12. SCHEDULER JOBS
================================================================================

services/scheduler_service.py
  BackgroundScheduler(timezone=MARKET_TIMEZONE)

Jobs:
  1. token_lifecycle_check
     interval: UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES
     function: check_upstox_token_validity(send_success_message=False)

  2. daily_refresh
     cron: DAILY_REFRESH_DAYS, DAILY_REFRESH_HOUR, DAILY_REFRESH_MINUTE
     function: token_tasks.daily_refresh.run_daily_refresh
     delegates to _run_full_refresh_sync("daily")

  3. first_candle_calc
     cron: DAILY_REFRESH_DAYS,
           FIRST_CANDLE_HOUR, FIRST_CANDLE_MINUTE, FIRST_CANDLE_SECOND
     function: token_tasks.first_candle_task.run_first_candle_calculation
     (Replaces the former opening_range_calc job.)

  4. readiness_digest                (READINESS_DIGEST_HOUR:MINUTE)
  5. preflight_recheck               (READINESS_PREFLIGHT_HOUR:MINUTE)

  6. level_isolation_recheck         (every 5 min, 9-15h)
     cron: */5 during 09:00-15:59 IST
     function: token_tasks.level_isolated_instrument_task
                 .run_level_isolation_recheck
     (Replaces strategy_selection_calc / isolation_calc.)

  7. daily_backup                    (DAILY_BACKUP_HOUR:MINUTE)

  8. live_sr_refresh_from_api        (every LIVE_SR_REFRESH_FROM_API_MINUTES)
     interval: LIVE_SR_REFRESH_FROM_API_MINUTES (default 2)
     function: _refresh_live_sr() -> sr_backfill_service.run(
                 reason="periodic_refresh",
                 send_telegram=False,
                 update_replay=False)                 <-- LIVE BUFFER ONLY
     The 2026-11-04 change: the periodic refresh updates only the
     live buffer. The replay series is frozen after the startup
     backfill so DUMMY_ORDER_DRIFT_GUARD can guarantee existing
     entries are never recomputed.
     The service is called with update_replay=False; if the service
     has not yet been updated to accept that kwarg, a TypeError is
     caught and the legacy call is used (with a loud warning).

  9. dummy_order_tracker             (every DUMMY_ORDER_TRACK_INTERVAL_SECONDS)
     interval: DUMMY_ORDER_TRACK_INTERVAL_SECONDS (default 30)
     function: _dummy_order_tick()                    <-- SHORT-CIRCUITS
       ready, reason = sr_backfill_ready()             <-- the latch
       if not ready:
           log "dummy_order_tracker skipped — <reason>"
           return
       dummy_order_service.run_once(reason="scheduled")

     The latch (`sr_backfill_ready()`) is the SAME one main.py uses.
     Any future tick that reaches dummy_order_service indirectly
     (e.g. a hypothetical _strategy2_recheck) MUST consult the same
     latch. The module-level helper sr_backfill_ready() and the
     singleton method scheduler_service.is_sr_backfill_ready() are
     both exposed for out-of-file consumers.

start() is idempotent.
shutdown() stops scheduler if running.

Defensive contract (documented in-file):
  Any job that invokes dummy_order_service — directly OR indirectly —
  MUST call sr_backfill_ready() first. Skipping the guard reopens the
  14:16-on-2026-10-29 class of bug (order placed against a partially-
  backfilled SR series on a mid-day restart).


================================================================================
13. HTTP / API ROUTE MAP
================================================================================

Page / UI:
  GET /                         index.html dashboard
  GET /charts                   instrument search page
  GET /chart/{instrument_key}   chart page
  GET /isolated                 isolated-instrument page
  GET /isolated-dashboard       redirect to /
  GET /dashboard                strategy dashboard
  GET /favicon.ico              204

Logs:
  GET /logs                     HTML log viewer
  GET /ui/logs                  JSON list of log files
  GET /ui/logs/{filename}       raw text log content

Upstox proxy:
  GET  /api/upstox/token
  GET  /api/upstox/search
  GET  /api/upstox/instrument
  GET  /api/upstox/intraday
  GET  /api/upstox/historical
  GET  /api/upstox/subscription-status
  POST /api/upstox/subscribe

Live REST:
  GET /live/status
  GET /live/instruments
  GET /live/instruments/resolve
  GET /live/crosses
  GET /live/crosses/{instrument_key}
  GET /live/ema
  GET /live/ema/{instrument_key}
  GET /live/ema-series
  GET /live/candles
  GET /live/snapshot
  GET /live/snapshot/{instrument_key}

Live SR calc (on-demand stateless SR):
  GET  /live-sr/calc
  GET  /live-sr/calc/{underlying}/{strike}/{type}
  POST /live-sr/calc/batch

Admin — core:
  POST /admin/hard-refresh
  GET  /admin/backup-status
  GET  /admin/cleanup-status
  POST /admin/cleanup

Admin — folders:
  GET /admin/folders
  GET /admin/folders/{folder_name}
  GET /admin/folders/{folder_name}/download

Admin — Level isolation:
  GET  /admin/level-isolation
  GET  /admin/level-isolation/detail
  GET  /admin/level-isolation/strategy-signal
  GET  /admin/level-isolation/sr-tracking
  POST /admin/level-isolation/replay           (replay backfill)
  POST /admin/level-isolation/refresh-snapshot (snapshot-only backfill)
  POST /admin/level-isolation/recalculate      (re-run isolation)

Admin — Simulation (diagnostic; NOT order placement):
  POST /admin/simulation/run

Admin — Market holiday:
  GET  /admin/market-holiday
  GET  /admin/market-holiday/{date_str}
  GET  /admin/market-holiday/cache-status
  POST /admin/market-holiday/clear-cache

Admin — Orders:
  POST /admin/orders/buy
  POST /admin/orders/sell
  POST /admin/orders/exit-all
  GET  /admin/orders/runtime-status
  POST /admin/orders/runtime-enable
  POST /admin/orders/runtime-disable

Admin — Margin:
  POST /admin/orders/margin
  POST /admin/orders/margin/batch

Dashboard data:
  GET /ui/dashboard/strategy

WebSockets:
  /ws/live
  /ws/chart
  /all-feeds
  /ws/all-feeds
  /ws/ema-crosses
  /ws/ema


================================================================================
14. DATA / LOG / FILE LAYOUT
================================================================================

Project root:
  main.py
  core/config.py
  core/logger.py
  routes/
  services/
  token_tasks/
  upstox_services/
  templates/
  logs/
  logs_text/
  meta_data/
  data/
  .env
  .env.example

Runtime data:
  data/
    index/<underlying>/
      candles.json
      ema_crosses.json
      live_crosses.json
      first_candle.json                          <-- replaces opening_range.json
      level_instruments.json                     <-- per-index CE/PE strike list
      live_sr_levels.json                        <-- pivot-based SR
    nifty/<strike>_<TYPE>/
      candles.json
      ema_crosses.json
      live_crosses.json
      first_candle.json
      live_sr_levels.json
    sensex/<strike>_<TYPE>/
      ... (same layout as nifty)
    _adhoc/<safe_key>/
      first_candle.json
      live_sr_levels.json
    level_isolated_instrument.json               <-- isolation record
    level_isolated_instrument_sr.json            <-- pre/post-isolation SR
    dummy_orders.json                            <-- t_feed_v1 dummy orders
    simulation/                                  <-- replay diagnostics

Logs:
  logs/<module>.log              rotating by line count, 1000 lines, 3 backups
  logs_text/<module>.txt         append-only plain text mirror

Important log modules:
  main
  live_feed_service
  live_backfill_service
  live_ema_engine
  token_monitor
  token_store
  upstox_token_client
  telegram_token_bot
  margin_check
  place_order
  exit_all_positions
  order_runtime_service
  market_holiday_service
  first_candle_service
  level_instrument_filter_service
  level_isolated_instrument_service
  isolated_sr_tracker_service
  sr_backfill_service
  live_sr_levels_service
  live_sr_calc_service
  dummy_order_service

Cleanup default:
  remove: data, temp, downloads, output, __pycache__, .pytest_cache,
          htmlcov, test_logs
  preserve: meta_data, logs, logs_text, .git, .venv, venv, myenv,
            templates, routes, services, core, token_tasks,
            upstox_services, static, tests


================================================================================
15. CONFIGURATION MAP
================================================================================

core/config.py reads .env.

MongoDB / token:
  MONGO_URL / MONGO_URI
  MONGO_DB
  TOKENS_COLLECTION
  UPSTOX_TOKEN_DOC_ID
  UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES

Market time:
  MARKET_TIMEZONE
  MARKET_TIME_FORMAT

Telegram:
  TELEGRAM_ENABLED
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  TELEGRAM_TIMEOUT_SECONDS
  TELEGRAM_TOKEN_BOT_ENABLED
  TELEGRAM_TOKEN_BOT_POLL_SECONDS
  TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT
  TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT

Charts:
  CHART_MAX_BATCH_WORKERS
  CHART_LIVE_POLL_SECONDS

Backup:
  BACKUP_METADATA_COLLECTION
  BACKUP_METADATA_DOC_ID
  BACKUP_BLOB_COLLECTION
  BACKUP_BLOB_DOC_ID
  LIVE_DATA_DIR
  BACKUP_MAX_AGE_MINUTES

Upstox live feed:
  UPSTOX_WS_MODE
  UPSTOX_WS_AUTO_START
  UPSTOX_WS_RECONNECT_INTERVAL
  UPSTOX_WS_MAX_RETRIES

Health feed:
  HEALTH_FEED_LOG
  HEALTH_FEED_INSTRUMENT
  HEALTH_FEED_INSTRUMENT_INTERVAL

EMA:
  EMA_FAST_PERIOD
  EMA_SLOW_PERIOD
  EMA_ATR_PERIOD
  EMA_VOLUME_LOOKBACK
  EMA_SLOPE_LOOKBACK
  EMA_SEP_ATR_MIN
  EMA_VOLUME_MULT
  EMA_DISTANCE_ATR_MAX
  EMA_MIN_VALIDATION_SCORE
  EMA_ENABLE_VALIDATION

Live crosses:
  LIVE_CROSSES_FILENAME

Scheduler:
  DAILY_REFRESH_ENABLED
  DAILY_REFRESH_HOUR
  DAILY_REFRESH_MINUTE
  DAILY_REFRESH_DAYS

Cleanup:
  CLEANUP_REMOVE_FOLDERS
  CLEANUP_RESERVED_FOLDERS

First candle OR:
  FIRST_CANDLE_ENABLED
  FIRST_CANDLE_HOUR
  FIRST_CANDLE_MINUTE
  FIRST_CANDLE_SECOND
  FIRST_CANDLE_WORKERS
  FIRST_CANDLE_REST_FALLBACK_ENABLED
  FIRST_CANDLE_REST_TIMEOUT_SECONDS

Level-instrument filter:
  LEVEL_FILTER_ENABLED
  LEVEL_FILTER_INDEXES
  LEVEL_FILTER_STRIKE_STEP_NIFTY
  LEVEL_FILTER_STRIKE_STEP_SENSEX
  LEVEL_FILTER_STRIKE_STEP_BANKNIFTY
  LEVEL_FILTER_STRIKE_STEP_MIDCPNIFTY
  LEVEL_FILTER_STRIKE_STEPS_JSON
  LEVEL_FILTER_RANGE_MODE
  LEVEL_FILTER_INCLUDE_ATM_PLUS_MINUS
  LEVEL_FILTER_INCLUDE_INDEX_SR

Live SR levels:
  LIVE_SR_ENABLED
  LIVE_SR_TRACK_ALL_INSTRUMENTS
  LIVE_SR_LEFT_BARS
  LIVE_SR_RIGHT_BARS
  LIVE_SR_MAX_CANDLES
  LIVE_SR_FILENAME
  LIVE_SR_PERSIST_EVERY_N_CANDLES
  LIVE_SR_REFRESH_FROM_API_ENABLED
  LIVE_SR_REFRESH_FROM_API_MINUTES

SR backfill:
  SR_BACKFILL_ENABLED
  SR_BACKFILL_MAX_WORKERS
  SR_BACKFILL_START_HOUR
  SR_BACKFILL_START_MINUTE
  SR_BACKFILL_END_HOUR
  SR_BACKFILL_END_MINUTE
  SR_BACKFILL_INCLUDE_HISTORICAL

Multi-index surveillance:
  STRATEGY_MONITORED_INDICES
  STRATEGY_TOUCH_PRIORITY_ORDER
  STRATEGY_ROUTED_EXECUTION_INDEX
  STRATEGY_INDEX_PRIORITY           (derived)

Level isolation:
  LEVEL_ISOLATION_ENABLED
  LEVEL_ISOLATION_FILENAME
  LEVEL_ISOLATION_CHECK_INTERVAL_SECONDS
  LEVEL_ISOLATION_NIFTY_TICK_STALE_SECONDS
  LEVEL_ISOLATION_BACKFILL_WINDOW_START_HOUR
  LEVEL_ISOLATION_BACKFILL_WINDOW_START_MINUTE
  LEVEL_ISOLATION_BACKFILL_WINDOW_END_HOUR
  LEVEL_ISOLATION_BACKFILL_WINDOW_END_MINUTE
  LEVEL_ISOLATION_TELEGRAM_ENABLED
  LEVEL_ISOLATION_REPLAY_USE_SERIES_OR
  LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL
  STRATEGY_RELATIONAL_R3_BIAS
  STRATEGY_RELATIONAL_S3_BIAS

Isolated SR tracker:
  LEVEL_ISOLATION_SR_FILENAME
  LEVEL_ISOLATION_SR_TELEGRAM_ENABLED

Live SR calc:
  LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS
  LIVE_SR_CALC_MAX_CANDLES

Strategy entry signal — SIMULATION-ONLY (does NOT drive orders):
  STRATEGY_ENTRY_ENABLED
  STRATEGY_ENTRY_ALLOW_CROSSOVER
  STRATEGY_ENTRY_BUFFER_MINUTES
  STRATEGY_ENTRY_BLOCKED_BY_RESISTANCE
  STRATEGY_ENTRY_ALLOW_AUTO_REVERSE
  STRATEGY_ENTRY_PRIORITIZE_SUPPORT
  STRATEGY_ENTRY_PIVOT_LEFT_BARS
  STRATEGY_ENTRY_PIVOT_RIGHT_BARS
  STRATEGY_ENTRY_STOP_LTP_TIERS
  STRATEGY_ENTRY_STOP_TICK_ROUNDING
  STRATEGY_MAX_STOP_PCT
  STRATEGY_ENTRY_ALLOW_PROMOTION     (DORMANT)
  STRATEGY_ENTRY_ALLOW_RE_PROMOTION  (DORMANT)
  STRATEGY_ENTRY_REGISTER_COUNTER_SIDE
  STRATEGY_ENTRY_CANDLE_FALLBACK_ENABLED
  STRATEGY_ENTRY_TARGET_REQUIRE_POST_ENTRY
  STRATEGY_ENTRY_FREEZE_ENTRY_ON_PROMOTION

  REMOVED in G1 v4.1 (no reader — safe to delete from .env):
    STRATEGY_ENTRY_INCLUDE_PRETOUCH
    STRATEGY_ENTRY_ONLY_ONCE_PER_DAY
    STRATEGY_ENTRY_FIRST_TO_CONFIRM_WINS

Simulation:
  SIMULATION_ENABLED
  SIMULATION_USE_REGIME_TARGETS
  SIMULATION_STOP_MODE_HIGH
  SIMULATION_SAME_BAR_STOP_FIRST
  SIMULATION_TICK_ROUNDING

Dummy orders (t_feed_v1-market-engine-v3):
  DUMMY_ORDER_ENABLED
  DUMMY_ORDER_TRACK_INTERVAL_SECONDS
  DUMMY_ORDER_START_HOUR
  DUMMY_ORDER_START_MINUTE
  DUMMY_ORDER_EOD_HOUR
  DUMMY_ORDER_EOD_MINUTE
  DUMMY_ORDER_FILENAME
  DUMMY_ORDER_TELEGRAM_ENABLED

Dummy-order deterministic-replay guards (2026-11-04):       <-- NEW
  DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK            (false)
  DUMMY_ORDER_STRICT_DETERMINISTIC_REPLAY         (true)
  DUMMY_ORDER_WAIT_FOR_SR_BACKFILL                (true)
  DUMMY_ORDER_WAIT_FOR_SR_BACKFILL_TIMEOUT_SECONDS (120)
  DUMMY_ORDER_DISABLE_RUNTIME_PATH_SWITCH         (true)
  DUMMY_ORDER_DRIFT_GUARD                         (true)

Strategy order (t_feed_v1 tunables):
  STRATEGY_ORDER_STOP_LOSS_PCT                    (0.30)
  STRATEGY_ORDER_MILESTONE_GAIN_POINTS            (50.0)
  STRATEGY_ORDER_REVERSAL_CUTOFF_HOUR             (9)
  STRATEGY_ORDER_REVERSAL_CUTOFF_MINUTE           (30)

EMA calc:
  EMA_CALC_DEFAULT_LOOKBACK_DAYS
  EMA_CALC_MAX_CANDLES

Daily backup:
  DAILY_BACKUP_ENABLED
  DAILY_BACKUP_HOUR
  DAILY_BACKUP_MINUTE
  DAILY_BACKUP_DAYS
  DAILY_BACKUP_FOLDERS
  DAILY_BACKUP_MONGO_DB
  DAILY_BACKUP_COLLECTION
  DAILY_BACKUP_DOC_ID
  DAILY_BACKUP_MAX_BLOB_MB
  DAILY_BACKUP_SOURCE_LABEL

Readiness:
  READINESS_DIGEST_ENABLED / HOUR / MINUTE
  READINESS_PREFLIGHT_ENABLED / HOUR / MINUTE
  READINESS_CLOCK_DRIFT_MAX_SECONDS
  READINESS_DISK_FREE_MIN_GB
  READINESS_REGISTRY_MIN_COUNT
  READINESS_TOKEN_MAX_AGE_MINUTES
  READINESS_SMOKE_TEST_ENABLED

Deprecated / ignored:
  STRATEGY_EXT_PATH_B_ENABLED             (ignored; Path A/B removed)
  STRATEGY_EXT_TRACKED_UNDERLYINGS        (ignored)
  STRATEGY_EXT_LOT_SIZE_NIFTY             (ignored)
  STRATEGY_EXT_LOT_SIZE_SENSEX            (ignored)
  STRATEGY_EXT_PROFIT_ACTIVATION_RS       (ignored)
  STRATEGY_EXT_TRAIL_GAP_RS               (ignored)
  STRATEGY_EXT_TRAIL_FLOOR_RS             (ignored)
  STRATEGY_EXT_R3_BREAKOUT_ACTIVATION_ENABLED (ignored)
  STRATEGY_EXT_BEARISH_EXIT_ENABLED       (ignored)
  STRATEGY_EXT_BEARISH_CANDLES            (ignored)
  STRATEGY_EXT_LOCKDOWN_ON_TARGET         (ignored)
  STRATEGY_TOUCH_POLL_INTERVAL_SEC        (ignored)
  DUMMY_ORDER_CREATE_REGIME_VARIANT       (ignored)
  DUMMY_ORDER_MAX_ORDERS_PER_DAY          (ignored)
  DUMMY_ORDER_RECONCILE_MINUTES           (ignored)
  STRATEGY_STAGE_1_NAME / 2 / 3 / 4       (ignored — Strategy 1 removed)
  STRATEGY_2_STAGE_3_NAME / 4             (ignored — Strategy 2 removed)
  STRATEGY_BASE_DIR / STRATEGY_2_BASE_DIR (ignored)
  STRATEGY_INDEXES_JSON                   (ignored)


================================================================================
16. FUTURE MODIFICATION GUIDE
================================================================================

Add a new startup / refresh task:
  File: routes/admin_routes.py
  Function: _run_full_refresh_sync()
  Add step in the canonical order, wrap in try/except, add to result.

Add a new REST endpoint:
  1. Create/modify a file in routes/
  2. Define APIRouter / route
  3. Include router in main.py with app.include_router(...)

Add a new WebSocket:
  File: routes/live_routes.py
  Use live_broadcast.subscribe() + _pump_with_client_listener()
  Or add a polling loop like /ws/ema.

Change live feed extraction:
  File: services/live_feed_service.py
  Method: _extract_tick()

Change EMA / cross rules:
  Files: services/live_ema_engine.py + core/config.py
  Methods: _compute_cross(), _detect_cross(), _ema_series(), _atr(), _sma()
  Config: EMA_* variables

Change First Candle OR formula:
  File: services/first_candle_service.py
  Function: compute_levels()

Change First Candle REST fallback:
  File: services/first_candle_service.py
  Function: _fetch_first_candle_via_rest()
  Config: FIRST_CANDLE_REST_FALLBACK_ENABLED,
          FIRST_CANDLE_REST_TIMEOUT_SECONDS

Change the level-instrument filter:
  File: services/level_instrument_filter_service.py
  Config: LEVEL_FILTER_*

Change the level-isolation arbitration:
  Files:
    services/level_isolated_instrument_service.py
    core/config.py
  Config:
    STRATEGY_MONITORED_INDICES
    STRATEGY_TOUCH_PRIORITY_ORDER
    LEVEL_ISOLATION_*
    STRATEGY_RELATIONAL_R3_BIAS / S3_BIAS

Change the isolated-SR tracker:
  File: services/isolated_sr_tracker_service.py
  Config: LEVEL_ISOLATION_SR_*

Change the post-isolation order placement:
  File: services/dummy_order_service.py
  Config:
    DUMMY_ORDER_*     (execution cadence, determinism guards)
    STRATEGY_ORDER_*  (stop, milestone, reversal cutoff)

Change the SR backfill:
  File: services/sr_backfill_service.py
  Config: SR_BACKFILL_*
  IMPORTANT: keep update_replay=False for the every-2-minute live
  refresh (see services/scheduler_service.py::_refresh_live_sr). The
  startup backfill is the only path that should populate the replay
  series; otherwise DUMMY_ORDER_DRIFT_GUARD loses its guarantee.

Change the scheduler latch policy:
  File: services/scheduler_service.py
  Function: sr_backfill_ready()
  Any new tick that reaches dummy_order_service MUST call it first.

Change the SIMULATION-only STRATEGY_ENTRY_* diagnostics:
  File: services/strategy_entry_signal_service.py
  Config: STRATEGY_ENTRY_*
  NOTE: these flags do NOT affect order placement. Order placement
  lives entirely in services/dummy_order_service.py.

Change market holiday gate:
  Files:
    services/market_holiday_service.py
    routes/market_holiday_routes.py
    main.py   (_holiday_gate_for_startup)
  To gate additional scheduled tasks, wrap the task body in:
    if market_holiday_service.is_trading_holiday(): return

Change order runtime toggle:
  File: services/order_runtime_service.py
  Consumers:
    upstox_services/place_order.py
    upstox_services/exit_all_positions.py
    services/dummy_order_service.py
    routes/order_routes.py
    routes/margin_routes.py
    token_tasks/order_toggle_commands.py

Change lot sizes:
  File: upstox_services/lot_sizes.py
  Update LOT_SIZES and DEFAULT_LOT_SIZES_BY_EXCHANGE.
  lot_sizes.py drives place_order, margin_check, and dummy_order_service.

Change the security policy:
  File: upstox_services/_sanitise.py
  Update SECRET_KEYS and SECRET_MIN_LEN.
  Every order-side module imports from here, so a single change
  applies everywhere.

Change config validation:
  File: services/config_validation.py
  Add new checks for any new pair of conflicting flags, or for any
  flag whose "unsafe" value reopens a known bug class (see the
  DUMMY_ORDER_* checks already in place).

Change backup extraction:
  File: services/backup_data_service.py
  Methods: _blob_doc(), _metadata_doc(), _extract_bytes(), refresh()

Change instrument registry:
  File: services/instrument_registry.py
  Methods: load(), get(), resolve(), register_adhoc(), query()

Change token storage / validation:
  Files:
    services/token_service.py
    token_tasks/token_store.py
    token_tasks/token_monitor.py
    token_tasks/upstox_token_client.py
  Never log raw token.

Change Telegram commands:
  File: token_tasks/telegram_token_bot.py
  Method: handle_message()
  Also set_commands().
  For /orders* commands, edit token_tasks/order_toggle_commands.py.

Change scheduler jobs:
  File: services/scheduler_service.py
  Method: start()

Change UI:
  Files:
    templates/base.html
    templates/index.html
    templates/instrument_list.html
    templates/chart.html
    templates/show_logs.html
    templates/isolated_instrument.html

Change logging:
  File: core/logger.py
  Handlers:
    LineCountRotatingHandler -> logs/*.log
    PlainTextHandler -> logs_text/*.txt

Change cleanup:
  File: services/cleanup_service.py
  Config:
    CLEANUP_REMOVE_FOLDERS
    CLEANUP_RESERVED_FOLDERS


================================================================================
17. KNOWN RISKS / GOTCHAS
================================================================================

1. README / architecture.md were outdated before this revision.
   This document now reflects the code including the 2026-11-02
   order-strategy rewrite, the 2026-11-04 deterministic-replay
   guards, the level-isolation pipeline, the market holiday gate,
   and the order / margin surface.

2. Cleanup removes data/ before backup extraction.
   Startup order is intentional: cleanup -> token -> backup -> registry.
   Do not move backup before cleanup unless you change that policy.

3. Logs are deliberately not cleaned.
   Logger holds open handles into logs/ and logs_text/.

4. live_cross_store.reset_all() clears memory only.
   It does not delete disk live_crosses.json.

5. Live feed without token:
   RuntimeError("No Upstox access token available for live feed")
   Feed retries every UPSTOX_WS_RECONNECT_INTERVAL seconds.

6. live_backfill uses Upstox intraday REST per instrument.
   If token missing or Upstox rate-limits, it logs warnings and
   continues.

7. _run_full_refresh_sync is synchronous and called via
   asyncio.to_thread. Keep CPU/network blocking work inside it or
   inside services it calls.

8. Manual hard refresh passes loop=None.
   If the app is already running, startup already wired the loop.

9. Port mismatch:
   Procfile uses 8000.
   run.bat and start.sh use 8010.

10. Telegram bot is disabled by default unless TELEGRAM_ENABLED and
    TELEGRAM_TOKEN_BOT_ENABLED are true and credentials are set.

11. Upstox SDK on_close signature:
    Use on_close(*args, **kwargs). Keep it that way.

12. Port of orphan routes from earlier revisions — verify before
    relying:
      /health, /token/status — not defined; check before use.

13. First-candle / isolation race (RESOLVED 2026-10-26):
    A late 09:15 index candle used to leave isolation without an OR.
    Now closed by:
      - first_candle REST fallback
      - extended backfill window (09:17 -> 23:59:59 weekdays)
      - replay-backed isolation backfill
    If any of those are disabled via env, the old behaviour returns.

14. Level-isolation phantom touch (RESOLVED 2026-10-29):
    Mid-day restart could rebuild OR from one series and candles
    from another, producing a trigger_ltp that contradicted the OR
    range (trigger_ltp < R3 while OR-H > R3). Now closed by
    LEVEL_ISOLATION_REPLAY_USE_SERIES_OR and
    LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL. Leave both true unless
    you are bisecting a regression.

15. Dummy-order non-determinism (RESOLVED 2026-11-04):
    Before the 2026-11-04 guards the every-2-minute SR refresh
    reseeded the replay series, so a mid-day restart could produce
    different entries/stops/targets for the same day. Now closed by:
      - sr_backfill_service.run(..., update_replay=False) in
        scheduler_service._refresh_live_sr
      - DUMMY_ORDER_DRIFT_GUARD=true (never overwrite a stored entry)
      - DUMMY_ORDER_STRICT_DETERMINISTIC_REPLAY=true
        (skip unresolved entries; never substitute LTP)
      - DUMMY_ORDER_WAIT_FOR_SR_BACKFILL=true (gate on backfill)
      - scheduler _dummy_order_tick short-circuits on
        sr_backfill_ready()
    Setting any of these to false reintroduces the class of bug.

16. Order runtime toggle resets to ENABLED on restart by design.
    If you need persistence across restarts, move the flag into
    MongoDB (services/order_runtime_service.py is the only file to
    change).

17. place_order and margin_check never guess the lot size blindly.
    The resolution chain (explicit underlying -> registry -> key scan
    -> exchange default -> unit fallback) is deterministic. If a
    future instrument has a new lot size, add it to
    upstox_services/lot_sizes.LOT_SIZES; do not let unit_fallback
    reach Upstox, because Upstox will reject qty=1 for F&O.

18. No token or account identifier appears in any log line or API
    response. Do not add one — the sanitiser only redacts what it
    recognises; if you introduce a new credential field, add its key
    to upstox_services/_sanitise.SECRET_KEYS.

19. Market holiday gate treats provider errors as "not a holiday".
    This is intentional: a network outage must never silently skip a
    real trading day. If Upstox is down and you still want to skip,
    do it manually via /admin/hard-refresh controls or by setting
    DAILY_REFRESH_ENABLED=false.

20. STRATEGY_ENTRY_* flags no longer drive order placement.
    Since the 2026-11-02 rewrite they feed only the diagnostic
    consumers (POST /admin/simulation/run and GET
    /admin/level-isolation/strategy-signal). Changing them will not
    change any real or dummy order. Order placement is driven
    exclusively by DUMMY_ORDER_* and STRATEGY_ORDER_*.

21. STRATEGY_EXT_PATH_B_ENABLED is DEPRECATED and ignored.
    Path selection is now derived solely from the isolation record's
    trigger_index (governed by
    DUMMY_ORDER_DISABLE_RUNTIME_PATH_SWITCH). Removing the key from
    .env is safe and recommended.

22. Any future tick that reaches dummy_order_service indirectly MUST
    consult sr_backfill_ready() first. Skipping this guard reopens
    the 14:16-on-2026-10-29 class of bug. See the DEFENSIVE CONTRACT
    block in services/scheduler_service.py.


================================================================================
18. QUICK MENTAL MODEL
================================================================================

Startup (holiday gate first):
  Holiday check -> if holiday: scheduler + telegram only
                -> else: Clean -> token -> backup -> registry ->
                         pre-register core indexes ->
                         daily_refresh backfill marker ->
                         wire loop + cross handler ->
                         start feed ->
                         first_candle backfill (09:17 -> 23:59) ->
                         level_instrument_filter (CE/PE per index) ->
                         level-isolation backfill (replay candles) ->
                         scheduler -> telegram.

Live:
  Upstox WS -> extract tick -> EMA engine -> finalise candle ->
  detect cross -> save live_crosses.json -> broadcast crosses.
  Every tick also broadcasts feed snapshots.
  Same finalized candle feeds:
    live_sr_levels_service (pivot scan, persisted per instrument)
    level_isolated_instrument_service (R3/S3 touch arbitration)
    isolated_sr_tracker_service (new post-isolation pivots only)

Order placement (deterministic t_feed_v1):
  Scheduler fires _dummy_order_tick every 30 s ->
    if NOT sr_backfill_ready(): skip
    else: dummy_order_service.run_once(reason="scheduled")
      reads isolation record + frozen replay series + own OR grid
      applies STRATEGY_ORDER_* tunables
      guarded by DUMMY_ORDER_* determinism flags
      writes data/dummy_orders.json
      fires 2 Telegram messages per order (create, close)

Live order surface (separate from dummy orders):
  place_order / exit_all_positions / margin_check all check
  order_runtime_service first, read the token from token_service,
  convert lots via upstox_services.lot_sizes, and sanitise every
  Upstox response before returning or logging.

APIs:
  UI reads /live/* and /api/upstox/*.
  Isolated page reads /admin/level-isolation/*.
  Admin triggers /admin/hard-refresh.
  Logs at /logs and /ui/logs/*.
  Live SR at /live-sr/calc/*.
  Holiday at /admin/market-holiday*.
  Orders at /admin/orders/*.
  Margin at /admin/orders/margin*.
  Simulation at /admin/simulation/run.

Persistence:
  MongoDB: token + backup archive + daily backup.
  data/: candles.json, ema_crosses.json, live_crosses.json,
         first_candle.json, level_instruments.json,
         live_sr_levels.json, level_isolated_instrument.json,
         level_isolated_instrument_sr.json, dummy_orders.json,
         simulation/.
  logs/ + logs_text/: runtime logs.

Rule:
  _run_full_refresh_sync is the single source of truth for startup,
  daily refresh, and manual hard refresh.
  Any new lifecycle task should be added there first.

Determinism rule (2026-11-04):
  same trading day
  + same isolation record (bias side, T_iso, trigger_index)
  + same intraday-API response
  => same dummy orders, regardless of process start time.
  Enforced by the six DUMMY_ORDER_* guards, by the
  sr_backfill_ready() latch on every tick, and by the
  update_replay=False contract on the periodic SR refresh.

Security rule:
  No token, no account identifier, no Authorization header in any
  log line or any HTTP response. Upstox responses and error bodies
  are redacted by upstox_services._sanitise before they leave the
  process.

================================================================================
END OF FILE
================================================================================