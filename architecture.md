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
  - Opening Range (OR) level calculation
  - Live cross persistence and WebSocket broadcasting
  - Scheduler for token checks, daily refresh, OR calculation
  - Market holiday gate (Upstox Market Holidays & Timings API)
  - Strategy 1 pipeline (buffer filter → isolation → R2 activation → order)
  - Strategy 2 pipeline (S3-touch watcher → next-cross order)
  - Runtime order toggle (enable / disable live order placement)
  - Lot-based order surface: place_order (buy/sell), exit_all_positions,
    margin_check (ChargeApi.post_margin)
  - Admin + Telegram control of the above

IMPORTANT:
  README/architecture.md were partially outdated prior to this revision.
  The updated document below reflects the actual code as of the
  strategy-race fix, holiday gate, and order-surface work.

KEY ADDITIONS SINCE THE PREVIOUS REVISION
-----------------------------------------
1. Opening Range REST fallback for the late 09:15 index candle
   (services/opening_range_service.py).

2. Per-instrument and global OR retry in Stage 1
   (services/strategy_selection_service.py,
    routes/admin_routes.py).

3. Strategy 2 Telegram quiet gate at two layers
   (services/scheduler_service.py,
    token_tasks/strategy_2_isolation_task.py).

4. Market holiday service + gate + admin routes
   (services/market_holiday_service.py,
    routes/market_holiday_routes.py,
    main.py).

5. Runtime order toggle + order/margin HTTP surface + Telegram
   commands
   (services/order_runtime_service.py,
    upstox_services/_sanitise.py,
    upstox_services/lot_sizes.py,
    upstox_services/place_order.py,
    upstox_services/exit_all_positions.py,
    upstox_services/margin_check.py,
    routes/order_routes.py,
    routes/margin_routes.py,
    token_tasks/order_toggle_commands.py,
    token_tasks/telegram_token_bot.py).

6. Security policy: no access token or account identifier appears in
   any log line or API response. Provider error bodies and verbose
   responses are redacted by upstox_services._sanitise.sanitise_payload.


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
    _holiday_gate_for_startup()      <-- NEW
      if is_holiday: skip unified refresh
                     start scheduler + telegram bot
                     send "APPLICATION STARTED · HOLIDAY"
      else:
        asyncio.to_thread(_run_full_refresh_sync, "startup", loop)
        Telegram startup notification
  shutdown:
    Telegram stopping notification
    live_feed_service.stop()
    strategy_live_tracker_service.stop()
    strategy_2_live_tracker_service.stop()
    telegram_token_bot.stop()
    scheduler_service.shutdown()
    Telegram stopped notification


================================================================================
2. HIGH-LEVEL COMPONENT MAP
================================================================================

                         +-----------------------------+
                         |        Browser / UI         |
                         |  index, charts, logs pages  |
                         |  strategy dashboard         |
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
|    opening_range_routes.py     /admin/opening-range/*                          |
|    strategy_routes.py          /admin/strategy/*           (Strategy 1)        |
|    strategy_2_routes.py        /admin/strategy2/*          (Strategy 2)        |
|    dashboard_routes.py         /dashboard, /ui/dashboard/strategy              |
|    upstox_routes.py            /api/upstox/*                                   |
|    log_routes.py               /logs, /ui/logs, /ui/logs/{filename}            |
|    market_holiday_routes.py    /admin/market-holiday*                          |  <-- NEW
|    order_routes.py             /admin/orders/*             (buy/sell/exit)     |  <-- NEW
|    margin_routes.py            /admin/orders/margin*       (ChargeApi)         |  <-- NEW
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
|                                           /orders_status                      |  <-- NEW
|                                                                                |
|  Data / Registry:                                                              |
|    services/backup_data_service.py        Mongo blob → data/ zip extraction    |
|    services/instrument_registry.py        scan data/ for ema_crosses.json      |
|    services/cleanup_service.py            delete transient folders             |
|    services/market_holiday_service.py     Upstox holiday check + cache         |  <-- NEW
|    services/order_runtime_service.py      runtime order toggle (ON at start)   |  <-- NEW
|                                                                                |
|  Live pipeline:                                                                |
|    services/live_feed_service.py          Upstox WebSocket                     |
|    services/live_ema_engine.py            1-min candles + EMA cross detection  |
|    services/live_cross_store.py           live_crosses.json persistence        |
|    services/live_broadcast.py             in-process pub/sub                   |
|    services/live_backfill_service.py      seed EMA from backup + intraday      |
|                                                                                |
|  Opening Range:                                                                |
|    services/opening_range_service.py      OR levels + opening_range.json       |
|                                           + REST fallback for late 09:15       |  <-- NEW
|                                                                                |
|  Strategy 1:                                                                   |
|    services/strategy_selection_service.py     Stage 1 + OR retry               |  <-- NEW
|    services/strategy_isolation_service.py     Stage 2                          |
|    services/strategy_live_tracker_service.py  Stage 3 (R2 watcher)             |
|    services/strategy_order_service.py         Stage 4 (paper order)            |
|                                                                                |
|  Strategy 2:                                                                   |
|    services/strategy_2_isolation_service.py    Stage 3 (S3-touch)              |
|    services/strategy_2_live_tracker_service.py Stage 3 watcher thread          |
|    services/strategy_2_order_service.py        Stage 4 (next-cross)            |
|                                                                                |
|  Order surface (live Upstox orders):                                           |
|    upstox_services/_sanitise.py            shared redaction                    |  <-- NEW
|    upstox_services/lot_sizes.py            NIFTY 65, SENSEX 20, …              |  <-- NEW
|    upstox_services/place_order.py          OrderApiV3 MARKET buy/sell          |  <-- NEW
|    upstox_services/exit_all_positions.py   OrderApi.exit_positions()           |  <-- NEW
|    upstox_services/margin_check.py         ChargeApi.post_margin               |  <-- NEW
|                                                                                |
|  Daily backup:                                                                 |
|    services/daily_backup_service.py       zip folders → MongoDB                |
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
|    REST: MarketHolidaysAndTimingsApi.get_holiday                               |  <-- NEW
|    REST: OrderApiV3.place_order, OrderApi.exit_positions                       |  <-- NEW
|    REST: ChargeApi.post_margin                                                 |  <-- NEW
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

Holiday gate (NEW, before the unified refresh):
  app_lifespan -> _holiday_gate_for_startup()
    +-- market_holiday_service.get_today()
    +-- if is_holiday:
    |     logger: "Holiday gate: skipping unified refresh"
    |     scheduler_service.start()
    |     telegram_token_bot.start()
    |     Telegram "APPLICATION STARTED · HOLIDAY"
    |     yield   (no refresh, no feed, no strategy pipeline)
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
  +-- 6b. _ensure_core_and_strategy_indexes_registered()
  |        pre-registers NSE_INDEX|Nifty 50, BSE_INDEX|SENSEX,
  |        and every STRATEGY_INDEXES entry as ad-hoc entries
  |
  +-- 7. Wire loop + cross handler
  |       live_broadcast.set_loop(loop)
  |       live_feed_service.set_loop(loop)
  |       _wire_cross_handler()
  |         on_cross(entry, cross):
  |           live_cross_store.save(entry, cross)
  |           live_broadcast.publish_sync("crosses", entry, {"cross": cross})
  |
  +-- 8. live_cross_store.reset_all()
  |       clears in-memory state only; disk live_crosses.json remains
  |
  +-- 9. live_backfill_service.seed_all()
  |       for each registry instrument:
  |         read backup candles.json tail 500
  |         fetch today's Upstox intraday 1-min candles
  |         merge by minute
  |         live_ema_engine.seed(key, merged)
  |         live_ema_engine.scan_crosses(key, since today 00:00)
  |       uses ThreadPoolExecutor, max CHART_MAX_BATCH_WORKERS <= 8
  |
  +-- 10. opening_range_service.calculate_all()
  |        for each registry instrument:
  |          get candles from live_ema_engine
  |          find first candle minute 09:15 IST
  |          if not in engine:
  |            fetch today's intraday candles via Upstox REST           <-- NEW
  |            pick the 09:15 candle from the REST response             <-- NEW
  |          compute OR levels
  |          write opening_range.json next to ema_crosses.json
  |          record candle_source = live_engine | rest_intraday_fallback
  |
  +-- 10b. Strategy 1 · Stage 1 (with OR retry)                       <-- NEW
  |        _run_stage1_with_or_retry()
  |          run strategy_selection_service.run()
  |          if every index skipped with or_unavailable:*:
  |            opening_range_service.calculate_all()    (full recalc)
  |            strategy_selection_service.run()         (retry once)
  |          each _process_index retries its own OR up to
  |          STRATEGY_STAGE1_OR_RETRY_ATTEMPTS times with
  |          STRATEGY_STAGE1_OR_RETRY_SLEEP_SECONDS between attempts
  |
  +-- 10c. Strategy 1 · Stage 2
  |        if Stage 1 produced 0 contracts:
  |          skip Stage 2 with reason stage_1_empty                 <-- NEW
  |        else:
  |          strategy_isolation_service.run()
  |
  +-- 10d. Strategy 1 · Stage 3 tracker.start()
  |
  +-- 10e. Strategy 1 · Stage 3 catch-up (if past window and in catch-up)
  |
  +-- 10f. Strategy 1 · Stage 4 catch-up (if past 09:18 and in catch-up)
  |
  +-- 10g. Strategy 2 · Stage 3 tracker.start()
  |
  +-- 10h. Strategy 2 · Stage 3 snapshot
  |        strategy_2_isolation_service.run()
  |        (Telegram output for this snapshot is handled by
  |         token_tasks/strategy_2_isolation_task.py quiet gate; the
  |         unified refresh itself does not emit a zero-data message)  <-- NEW
  |
  +-- 10i. Strategy 2 · Stage 4 order
  |        only when stage2_contracts > 0 AND an isolated pick exists
  |
  +-- 11. live_feed_service.start() or restart()
  |
  +-- 12. scheduler_service.start()
  |        + strategy_2_stage3_recheck now points at _strategy2_recheck()
  |          which short-circuits when the stage-2 tree has no strike
  |          folders                                              <-- NEW
  |
  +-- 13. telegram_token_bot.start()
  |
  +-- 14. Telegram: "<Reason> Refresh Completed / Finished"
  |        Stage 1 line now carries (OR retry=✓/✗)
  |        Stage 2 line now carries "skipped — stage_1_empty" when
  |        that was the outcome                                 <-- NEW
  |
  +-- 15. return structured result (includes holiday_gate,
           strategy_stage1_or_retry, strategy_stage1_contracts,
           stage2_skip_reason)
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
  strategy_live_tracker_service.stop()
  strategy_2_live_tracker_service.stop()
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
                                       /orders_disable                <-- NEW

Token flow: unchanged.

Telegram order-toggle commands (NEW):
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
8. OPENING RANGE (OR) FLOW
================================================================================

Trigger:
  - scheduler job "opening_range_calc" at OPENING_RANGE_HOUR:MINUTE:SECOND
    default 09:16:10 IST
  - startup / daily refresh / hard refresh also call calculate_all()

Flow:

opening_range_service.calculate_all()
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
    |     if OPENING_RANGE_REST_FALLBACK_ENABLED:                   <-- NEW
    |       _fetch_first_candle_via_rest(key, target_minute)        <-- NEW
    |       if found: use it; candle_source = rest_intraday_fallback <-- NEW
    |     else:
    |       skipped: first_candle_not_available
    |
    v
  compute_levels(high, low)
    H, L, A, D_H, D_L, R1, S1, R2, S2, R3, S3,
    R3_Threshold, S3_Threshold
    |
    v
  write opening_range.json next to ema_crosses.json
    or data/_adhoc/<safe_key>/opening_range.json
    persist candle_source                                            <-- NEW
    |
    v
  return status ok/skipped/error

API:
  GET  /admin/opening-range
  GET  /admin/opening-range/{instrument_key}
  POST /admin/opening-range/recalculate
  POST /admin/opening-range/recalculate/{instrument_key}

REST fallback policy (NEW):
  - Timeout: OPENING_RANGE_REST_TIMEOUT_SECONDS (default 15).
  - Uses the existing Upstox access token via token_service.
  - Never logs or returns the token.
  - Only fires when the in-memory EMA engine does not yet have the
    09:15 candle, so the live-feed path is unchanged on fast days.


================================================================================
9. STRATEGY PIPELINE
================================================================================

Strategy 1 (unchanged stages, plus OR retry in Stage 1):

  Stage 1  strategy_selection_service.run()
           + per-instrument OR retry before skipping            <-- NEW
  Stage 2  strategy_isolation_service.run()
           + skipped with reason="stage_1_empty" when Stage 1
             produced 0 contracts across all indexes            <-- NEW
  Stage 3  strategy_live_tracker_service.start() / run_catchup()
  Stage 4  strategy_order_service.run()

Strategy 2 (unchanged stages, plus Telegram quiet gate):

  Stage 3  strategy_2_isolation_service.run()
           + live watcher thread runs all day
           + Telegram output gated on state change              <-- NEW
           + scheduler recheck self-gates on empty stage-2 tree <-- NEW
  Stage 4  strategy_2_order_service.run()
           + only when stage2_contracts > 0 AND an isolated
             pick exists                                        <-- NEW

Strategy 2 Telegram quiet gate (NEW):
  Layer 1  services/scheduler_service._strategy2_recheck()
           short-circuits when _stage2_tree_has_contracts() is False
           (no strike folders under STRATEGY_BASE_DIR/STRATEGY_STAGE_2_NAME).
           No stage-2 contracts -> no recheck at all.
  Layer 2  token_tasks/strategy_2_isolation_task.run_strategy_2_isolation()
           - returns early with one INFO log line when
             stage2_contracts == 0
           - otherwise sends a completion message only when the
             isolated pick changed OR a new touch was recorded
           - errors still send a "Failed" Telegram message at ERROR
  Config   STRATEGY_2_TELEGRAM_QUIET_WHEN_EMPTY (default True)


================================================================================
10. MARKET HOLIDAY GATE (NEW)
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

Note:
  Reuse the gate in scheduled tasks (daily refresh, OR, Stage 1) if
  you want them to skip holidays too:
      if market_holiday_service.is_trading_holiday():
          logger.info("<task>: market holiday — skipping")
          return


================================================================================
11. ORDER SURFACE (NEW)
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

Place order:
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

Exit all positions:
  upstox_services/exit_all_positions.py
    exit_all_positions(dry_run=False) -> dict
    Wraps OrderApi.exit_positions().
    Gated on order_runtime_service.is_enabled().
    CLI: python -m upstox_services.exit_all_positions [--dry-run]

Margin check:
  upstox_services/margin_check.py
    check_margin(
        instrument_key,
        lots=1,
        transaction_type="BUY",
        product="D",
        price=0.0,
        verbose=False,          # default -> compact response
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

Security policy (NEW):
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

  3. opening_range_calc
     cron: DAILY_REFRESH_DAYS,
           OPENING_RANGE_HOUR, OPENING_RANGE_MINUTE, OPENING_RANGE_SECOND
     function: token_tasks.opening_range_task.run_opening_range_calculation

  4. strategy_selection_calc       (OR + 20s)
  5. strategy_isolation_calc       (OR + 50s)
  6. strategy_order_calc           (STRATEGY_ORDER_HOUR:MINUTE)

  7. strategy_2_stage3_isolation   (09:18:15)
  8. strategy_2_stage3_recheck     (every 5 minutes, 9-15h)   <-- CHANGED
       Now points at _strategy2_recheck(), which short-circuits when
       the stage-2 tree has no strike folders. No stage-2 contracts
       -> no recheck at all -> no zero-data Telegram messages.
  9. strategy_2_stage4_order       (15:25:00)

 10. daily_backup                  (DAILY_BACKUP_HOUR:MINUTE)

start() is idempotent.
shutdown() stops scheduler if running.


================================================================================
13. HTTP / API ROUTE MAP
================================================================================

Page / UI:
  GET /                         index.html dashboard
  GET /charts                   instrument search page
  GET /chart/{instrument_key}   chart page
  GET /dashboard                strategy dashboard
  GET /isolated-dashboard       redirect to /
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

Admin — core:
  POST /admin/hard-refresh
  GET  /admin/backup-status
  GET  /admin/cleanup-status
  POST /admin/cleanup

Admin — folders:
  GET /admin/folders
  GET /admin/folders/{folder_name}
  GET /admin/folders/{folder_name}/download

Admin — Opening Range:
  GET  /admin/opening-range
  GET  /admin/opening-range/{instrument_key}
  POST /admin/opening-range/recalculate
  POST /admin/opening-range/recalculate/{instrument_key}

Admin — Strategy 1:
  GET  /admin/strategy
  GET  /admin/strategy/indexes
  GET  /admin/strategy/{ticker}
  GET  /admin/strategy/{ticker}/strikes
  POST /admin/strategy/recalculate
  GET  /admin/strategy/isolation
  GET  /admin/strategy/isolation/{ticker}
  POST /admin/strategy/isolation/recalculate
  GET  /admin/strategy/tracker/status
  POST /admin/strategy/tracker/start
  POST /admin/strategy/tracker/stop
  GET  /admin/strategy/stage3
  GET  /admin/strategy/stage3/{ticker}
  GET  /admin/strategy/stage4
  GET  /admin/strategy/stage4/{ticker}
  POST /admin/strategy/order/run

Admin — Strategy 2:
  GET  /admin/strategy2
  GET  /admin/strategy2/tracker/status
  POST /admin/strategy2/tracker/start
  POST /admin/strategy2/tracker/stop
  POST /admin/strategy2/stage3/run
  GET  /admin/strategy2/stage4
  POST /admin/strategy2/stage4/run

Admin — Market holiday (NEW):
  GET  /admin/market-holiday
  GET  /admin/market-holiday/{date_str}
  GET  /admin/market-holiday/cache-status
  POST /admin/market-holiday/clear-cache

Admin — Orders (NEW):
  POST /admin/orders/buy
  POST /admin/orders/sell
  POST /admin/orders/exit-all
  GET  /admin/orders/runtime-status
  POST /admin/orders/runtime-enable
  POST /admin/orders/runtime-disable

Admin — Margin (NEW):
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
  upstox_services/                <-- NEW
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
      opening_range.json
    nifty/<strike>_<TYPE>/
      candles.json
      ema_crosses.json
      live_crosses.json
      opening_range.json
    sensex/<strike>_<TYPE>/
      ...
    _adhoc/<safe_key>/
      opening_range.json
    strategy/                     Strategy 1 output
    strategy2/                    Strategy 2 output
    simulation/                   Simulation replay output

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
  margin_check                    <-- NEW
  place_order                     <-- NEW
  exit_all_positions              <-- NEW
  order_runtime_service           <-- NEW
  market_holiday_service          <-- NEW

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

Opening Range:
  OPENING_RANGE_FILENAME
  OPENING_RANGE_FIRST_MINUTE
  OPENING_RANGE_HOUR
  OPENING_RANGE_MINUTE
  OPENING_RANGE_SECOND
  OPENING_RANGE_REST_FALLBACK_ENABLED       <-- NEW
  OPENING_RANGE_REST_TIMEOUT_SECONDS        <-- NEW

Strategy 1:
  STRATEGY_ENABLED
  STRATEGY_BASE_DIR
  STRATEGY_STAGE_1_NAME
  STRATEGY_STAGE_2_NAME
  STRATEGY_STAGE_3_NAME
  STRATEGY_STAGE_4_NAME
  STRATEGY_BUFFER_MODE
  STRATEGY_INDEXES_JSON
  STRATEGY_FETCH_HISTORY
  STRATEGY_HISTORY_DAYS
  STRATEGY_MAX_WORKERS
  STRATEGY_ISOLATION_ENABLED
  STRATEGY_ISOLATION_LOOKBACK_MINUTES
  STRATEGY_ISOLATION_SKIP_UNRESOLVED
  STRATEGY_TRACKER_ENABLED
  STRATEGY_TRACKER_WINDOW_START
  STRATEGY_TRACKER_WINDOW_END
  STRATEGY_TRACKER_POLL_SECONDS
  STRATEGY_ORDER_ENABLED
  STRATEGY_ORDER_NIFTY_PICKS
  STRATEGY_ORDER_SENSEX_PICKS
  STRATEGY_ORDER_FINAL_PICKS
  STRATEGY_ORDER_HOUR
  STRATEGY_ORDER_MINUTE
  STRATEGY_ORDER_QTY
  STRATEGY_TRACKER_INDEX_KEYS
  STRATEGY_VERBOSE_LOG
  STRATEGY_STAGE1_OR_RETRY_ATTEMPTS         <-- NEW
  STRATEGY_STAGE1_OR_RETRY_SLEEP_SECONDS    <-- NEW
  STRATEGY_STAGE1_GLOBAL_OR_RETRY           <-- NEW

Strategy 2:
  STRATEGY_2_ENABLED
  STRATEGY_2_BASE_DIR
  STRATEGY_2_ISOLATION_LEVEL
  STRATEGY_2_TICKER_PRIORITY
  STRATEGY_2_TRACKER_ENABLED
  STRATEGY_2_TRACKER_WINDOW_START
  STRATEGY_2_TRACKER_WINDOW_END
  STRATEGY_2_TRACKER_POLL_SECONDS
  STRATEGY_2_ORDER_ENABLED
  STRATEGY_2_ORDER_QTY
  STRATEGY_2_STAGE_3_NAME
  STRATEGY_2_STAGE_4_NAME
  STRATEGY_2_TELEGRAM_QUIET_WHEN_EMPTY      <-- NEW

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

Simulation replay:
  SIMULATION_ENABLED
  SIMULATION_BASE_DIR
  SIMULATION_S2_CROSS_BUFFER_MINUTES


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

Change Opening Range formula:
  File: services/opening_range_service.py
  Function: compute_levels()

Change Opening Range REST fallback:                            <-- NEW
  File: services/opening_range_service.py
  Function: _fetch_first_candle_via_rest()
  Config: OPENING_RANGE_REST_FALLBACK_ENABLED,
          OPENING_RANGE_REST_TIMEOUT_SECONDS

Change Stage-1 OR retry:                                       <-- NEW
  Files:
    services/strategy_selection_service.py  (_process_index)
    routes/admin_routes.py                  (_run_stage1_with_or_retry)
  Config:
    STRATEGY_STAGE1_OR_RETRY_ATTEMPTS
    STRATEGY_STAGE1_OR_RETRY_SLEEP_SECONDS
    STRATEGY_STAGE1_GLOBAL_OR_RETRY

Change Strategy 2 Telegram quiet gate:                         <-- NEW
  Files:
    services/scheduler_service.py           (_strategy2_recheck)
    token_tasks/strategy_2_isolation_task.py
  Config:
    STRATEGY_2_TELEGRAM_QUIET_WHEN_EMPTY

Change market holiday gate:                                    <-- NEW
  Files:
    services/market_holiday_service.py
    routes/market_holiday_routes.py
    main.py                                 (_holiday_gate_for_startup)
  To gate additional scheduled tasks, wrap the task body in:
    if market_holiday_service.is_trading_holiday(): return

Change order runtime toggle:                                   <-- NEW
  File: services/order_runtime_service.py
  Consumers:
    upstox_services/place_order.py
    upstox_services/exit_all_positions.py
    routes/order_routes.py
    routes/margin_routes.py (indirectly via place_order)
    token_tasks/order_toggle_commands.py

Change lot sizes:                                              <-- NEW
  File: upstox_services/lot_sizes.py
  Update LOT_SIZES and DEFAULT_LOT_SIZES_BY_EXCHANGE.
  lot_sizes.py drives both place_order and margin_check.

Change the security policy:                                    <-- NEW
  File: upstox_services/_sanitise.py
  Update SECRET_KEYS and SECRET_MIN_LEN.
  Every order-side module imports from here, so a single change
  applies everywhere.

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
    templates/strategy_dashboard.html

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

1. README / architecture.md were outdated prior to this revision.
   This document now reflects the actual code including Strategy 1,
   Strategy 2, the market holiday gate, and the order / margin
   surface.

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

12. Orphan routes: none known after this revision.
    /health and /token/status are still not defined; verify before
    relying on them.

13. OR / Stage-1 race (RESOLVED):
    Previously, a late 09:15 index candle caused Stage 1 to skip with
    or_unavailable:not_calculated. Now closed by:
      - OR REST fallback in opening_range_service
      - per-instrument OR retry in strategy_selection_service
      - global OR retry in admin_routes._run_stage1_with_or_retry
    If any of those are disabled via env, the old behaviour returns.

14. Strategy 2 Telegram flood (RESOLVED):
    The 5-minute recheck no longer sends zero-data messages when the
    stage-2 tree is empty, and the scheduled task only sends a
    completion message when the isolated pick changed or a new touch
    was recorded. Set STRATEGY_2_TELEGRAM_QUIET_WHEN_EMPTY=false only
    if you explicitly want the old behaviour.

15. Order runtime toggle resets to ENABLED on restart by design.
    If you need persistence across restarts, move the flag into
    MongoDB (services/order_runtime_service.py is the only file to
    change).

16. place_order and margin_check never guess the lot size blindly.
    The resolution chain (explicit underlying -> registry -> key scan
    -> exchange default -> unit fallback) is deterministic. If a
    future instrument has a new lot size, add it to
    upstox_services/lot_sizes.LOT_SIZES; do not let unit_fallback
    reach Upstox, because Upstox will reject qty=1 for F&O.

17. No token or account identifier appears in any log line or API
    response. Do not add one — the sanitiser only redacts what it
    recognises; if you introduce a new credential field, add its key
    to upstox_services/_sanitise.SECRET_KEYS.

18. Market holiday gate treats provider errors as "not a holiday".
    This is intentional: a network outage must never silently skip a
    real trading day. If Upstox is down and you still want to skip,
    do it manually via /admin/hard-refresh controls or by setting
    DAILY_REFRESH_ENABLED=false.


================================================================================
18. QUICK MENTAL MODEL
================================================================================

Startup (holiday gate first):
  Holiday check -> if holiday: scheduler + telegram only
                -> else: Clean -> token -> backup -> registry ->
                         wire -> reset crosses -> backfill EMA ->
                         calculate OR (with REST fallback) ->
                         Stage 1 (with OR retry) ->
                         Stage 2 (skipped if Stage 1 empty) ->
                         Stage 3 tracker -> Stage 3 catch-up ->
                         Stage 4 catch-up ->
                         Strategy 2 tracker ->
                         Strategy 2 Stage 3 snapshot (quiet gate) ->
                         Strategy 2 Stage 4 (only if isolated) ->
                         start feed -> scheduler -> telegram.

Live:
  Upstox WS -> extract tick -> EMA engine -> finalise candle ->
  detect cross -> save live_crosses.json -> broadcast crosses.
  Every tick also broadcasts feed snapshots.

Order surface:
  place_order / exit_all_positions / margin_check all check
  order_runtime_service first, read the token from token_service,
  convert lots via upstox_services.lot_sizes, and sanitise every
  Upstox response before returning or logging.

APIs:
  UI reads /live/* and /api/upstox/*.
  Admin triggers /admin/hard-refresh.
  Logs at /logs and /ui/logs/*.
  OR at /admin/opening-range/*.
  Holiday at /admin/market-holiday*.
  Orders at /admin/orders/*.
  Margin at /admin/orders/margin*.
  Strategy at /admin/strategy/* and /admin/strategy2/*.

Persistence:
  MongoDB: token + backup archive + daily backup.
  data/: candles.json, ema_crosses.json, live_crosses.json,
         opening_range.json, strategy/, strategy2/, simulation/.
  logs/ + logs_text/: runtime logs.

Rule:
  _run_full_refresh_sync is the single source of truth for startup,
  daily refresh, and manual hard refresh.
  Any new lifecycle task should be added there first.

Security rule:
  No token, no account identifier, no Authorization header in any
  log line or any HTTP response. Upstox responses and error bodies
  are redacted by upstox_services._sanitise before they leave the
  process.

================================================================================
END OF FILE
================================================================================