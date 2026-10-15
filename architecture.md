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

IMPORTANT:
  The README/architecture.md are partially outdated. The actual code still
  contains EMA, Opening Range, live feed, live backfill, live crosses, and
  related WebSockets.


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
    asyncio.to_thread(_run_full_refresh_sync, "startup", loop)
    Telegram startup notification
  shutdown:
    Telegram stopping notification
    live_feed_service.stop()
    telegram_token_bot.stop()
    scheduler_service.shutdown()
    Telegram stopped notification


================================================================================
2. HIGH-LEVEL COMPONENT MAP
================================================================================

                         +-----------------------------+
                         |        Browser / UI         |
                         |  index, charts, logs pages  |
                         +--------------+--------------+
                                        |
                                        | HTTP / WebSocket
                                        v
+--------------------------------------------------------------------------------+
|                              FastAPI main.py                                   |
|                                                                                |
|  Routers:                                                                      |
|    page_routes.py          /, /charts, /chart/{instrument_key}, /favicon.ico    |
|    live_routes.py          /live/*, /ws/live, /ws/chart, /all-feeds, /ws/ema-*  |
|    admin_routes.py         /admin/hard-refresh, /admin/backup-status, cleanup  |
|    folder_routes.py        /admin/folders, /admin/folders/{name}/download       |
|    opening_range_routes.py /admin/opening-range/*                               |
|    upstox_routes.py        /api/upstox/*                                        |
|    log_routes.py           /logs, /ui/logs, /ui/logs/{filename}                 |
+------------------------------------+-------------------------------------------+
                                     |
                                     v
+--------------------------------------------------------------------------------+
|                                  Services                                      |
|                                                                                |
|  Token / Telegram:                                                             |
|    services/token_service.py          cached Upstox token                       |
|    token_tasks/token_store.py         MongoDB token document                    |
|    token_tasks/token_monitor.py       token validity checks                     |
|    token_tasks/upstox_token_client.py Upstox profile/funds validation           |
|    services/telegram_service.py       notifications                             |
|    token_tasks/telegram_token_bot.py  /save_token, /token_status, etc.          |
|                                                                                |
|  Data / Registry:                                                              |
|    services/backup_data_service.py    Mongo blob -> data/ zip extraction         |
|    services/instrument_registry.py    scan data/ for ema_crosses.json           |
|    services/cleanup_service.py        delete transient folders                  |
|                                                                                |
|  Live pipeline:                                                                |
|    services/live_feed_service.py      Upstox WebSocket                          |
|    services/live_ema_engine.py        1-min candles + EMA cross detection       |
|    services/live_cross_store.py       live_crosses.json persistence             |
|    services/live_broadcast.py         in-process pub/sub hub                    |
|    services/live_backfill_service.py  seed EMA from backup + intraday           |
|                                                                                |
|  Opening Range:                                                                |
|    services/opening_range_service.py  OR levels + opening_range.json            |
|                                                                                |
|  Scheduling:                                                                   |
|    services/scheduler_service.py      APScheduler singleton                     |
+------------------------------------+-------------------------------------------+
                                     |
                                     v
+--------------------------------------------------------------------------------+
|                              External Systems                                  |
|                                                                                |
|  MongoDB:                                                                      |
|    MONGO_DB.TOKENS_COLLECTION          Upstox token document                   |
|    MONGO_DB.BACKUP_METADATA_COLLECTION candle-backup metadata                   |
|    MONGO_DB.BACKUP_BLOB_COLLECTION     candle-backup archive blob               |
|                                                                                |
|  Upstox:                                                                       |
|    REST: profile, search, historical, intraday                                 |
|    WebSocket: MarketDataStreamerV3                                             |
|                                                                                |
|  Telegram:                                                                     |
|    notifications + token management bot                                       |
+--------------------------------------------------------------------------------+


================================================================================
3. STARTUP / UNIFIED REFRESH FLOW
================================================================================

All three refresh triggers use the SAME routine:
  - Project startup      -> main.app_lifespan -> _run_full_refresh_sync("startup")
  - Daily refresh        -> token_tasks/daily_refresh.py -> _run_full_refresh_sync("daily")
  - Manual hard refresh  -> POST /admin/hard-refresh -> _run_full_refresh_sync("manual")

Flowchart:

uvicorn main:app
  |
  v
app_lifespan
  |
  v
asyncio.to_thread(_run_full_refresh_sync, reason, loop)
  |
  +-- 1. Telegram: "<Reason> Refresh Started"
  |
  +-- 2. cleanup_service.run(reason)
  |       removes CLEANUP_REMOVE_FOLDERS inside project root
  |       preserves CLEANUP_RESERVED_FOLDERS
  |       default remove: data,temp,downloads,output,__pycache__,
  |                       .pytest_cache,htmlcov,test_logs
  |       default preserve: meta_data,logs,logs_text,.git,.venv,venv,
  |                         myenv,templates,routes,services,core,
  |                         token_tasks,static,tests
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
  |          compute OR levels
  |          write opening_range.json next to ema_crosses.json
  |
  +-- 11. live_feed_service.start() or restart()
  |        if already running: restart()
  |        else: start()
  |        builds subscription list from registry + core indices
  |        + HEALTH_FEED_INSTRUMENT
  |
  +-- 12. scheduler_service.start()
  |        idempotent APScheduler BackgroundScheduler
  |
  +-- 13. telegram_token_bot.start()
  |        idempotent long-polling worker if enabled
  |
  +-- 14. Telegram: "<Reason> Refresh Completed / Finished"
  |
  +-- 15. return structured result
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
| instrument_registry.get()        |
| or register_adhoc()              |
+----------------------------------+
           |
           v
+----------------------------------+
| _extract_tick(feed)              |
| Supports:                        |
|   fullFeed / ff / indexFF /      |
|   marketFF / oc / marketLevel    |
| Extracts:                        |
|   ltp, prev_close, volume,       |
|   candle_1m = interval "I1"      |
+----------------------------------+
           |
           +-------------------------------+
           |                               |
           v                               v
+------------------------+      +-------------------------------+
| Health feed logging    |      | live_ema_engine.on_tick()     |
| if HEALTH_FEED_LOG and |      | key, candle_1m, ltp, volume   |
| instrument matches     |      +---------------+---------------+
| HEALTH_FEED_INSTRUMENT |                      |
+------------------------+                      v
                                     +-------------------------------+
                                     | live_broadcast.publish_sync   |
                                     | topic "feed"                  |
                                     | -> /ws/live                   |
                                     | -> /ws/chart                  |
                                     | -> /all-feeds                 |
                                     +-------------------------------+

EMA ENGINE INTERNAL FLOW:

on_tick(key, candle_1m, ltp, volume)
  |
  v
_ingest(entry, candle_1m, ltp, volume)
  |
  v
_normalise() -> (iso_ts, o, h, l, c, v)
  |
  v
minute_key = iso_ts[:16]  // YYYY-MM-DDTHH:MM
  |
  +-- if no forming candle or minute changed:
  |     if previous forming exists: _finalise(previous)
  |     create new forming candle
  |
  +-- else same minute:
        merge high/low/close/volume into forming candle

_finalise(entry, candle)
  |
  v
append candle to rolling _candles[key], max 500
  |
  v
_detect_cross(entry, candles)
  |
  v
_compute_cross()
  |
  +-- compute fast EMA, slow EMA, ATR, avg volume
  +-- detect bullish cross: prev_diff <= 0 < now_diff
  +-- detect bearish cross: prev_diff >= 0 > now_diff
  +-- build six filters:
  |     1 trend
  |     2 separation vs ATR
  |     3 slope agreement
  |     4 volume
  |     5 body close beyond slow EMA
  |     6 distance from slow EMA vs ATR
  +-- score / max_score / valid
  |
  v
if cross and _cross_handler is set:
  _wire_cross_handler.on_cross(entry, cross)
    |
    +-- live_cross_store.save(entry, cross)
    |     writes/updates data/.../live_crosses.json
    |
    +-- live_broadcast.publish_sync("crosses", entry, {"cross": cross})
          -> /ws/ema-crosses clients


================================================================================
5. WEBSOCKET / BROADCAST ARCHITECTURE
================================================================================

services/live_broadcast.py
  In-process pub/sub hub.
  Topics:
    "feed"    -> live tick snapshots
    "crosses" -> EMA cross records

  Subscriber filters:
    - explicit instrument_keys
    - triples (underlying, strike, type)
    - all_instruments

  Async API:
    await live_broadcast.publish(topic, entry, payload)

  Sync API for worker threads:
    live_broadcast.publish_sync(topic, entry, payload)
    Requires live_broadcast.set_loop(asyncio_loop) at startup.

WebSocket routes in routes/live_routes.py:
  /ws/live          feed snapshots, filtered
  /ws/chart         chart feed snapshots
  /all-feeds        all instruments feed
  /ws/all-feeds     alias
  /ws/ema-crosses   cross events
  /ws/ema           polling EMA snapshot for one instrument

Client listener:
  _pump_with_client_listener(ws, sub)
    - sends queued broadcast messages
    - concurrently receives client JSON
    - supports {"action":"subscribe","instrument_key":"..."}
    - calls live_feed_service.subscribe_instrument()


================================================================================
6. BACKUP EXTRACTION + INSTRUMENT REGISTRY
================================================================================

MongoDB candle-backup documents:

  Metadata doc:
    BACKUP_METADATA_COLLECTION = app_tokens
    BACKUP_METADATA_DOC_ID     = backup_candles

  Blob doc:
    BACKUP_BLOB_COLLECTION = backup_collection
    BACKUP_BLOB_DOC_ID     = backup_historical_candles

Flow:

backup_data_service.refresh()
  |
  v
read blob doc from MongoDB
  |
  +-- no blob doc -> status no_blob_doc
  |
  +-- blob has no "data" -> status blob_missing
  |     logs MEGA file_link for manual download
  |
  +-- blob has "data" Binary/bytes
        |
        v
      write temp zip
      validate zipfile
      extract to temp stage
      replace data/ atomically-ish
      count files
      return summary

Extracted data tree expected:
  data/
    index/<underlying>/candles.json
    index/<underlying>/ema_crosses.json
    nifty/<strike>_<TYPE>/candles.json
    nifty/<strike>_<TYPE>/ema_crosses.json
    sensex/<strike>_<TYPE>/candles.json
    sensex/<strike>_<TYPE>/ema_crosses.json

instrument_registry.load()
  |
  v
root = config.LIVE_DATA_DIR / data
  |
  v
rglob("ema_crosses.json")
  |
  v
for each file:
  read JSON payload
  build entry:
    instrument_key
    underlying
    strike_price
    strike_type
    expiry
    trading_symbol
    candles_path = same folder / candles.json
    ema_path = this file
    is_index = key starts NSE_INDEX| or BSE_INDEX|
  |
  v
store in _by_key, _by_triple, _ordered

Registry supports:
  get(key)
  resolve(underlying, strike, strike_type)
  all()
  subscribe_keys()
  query(underlying, strike_type, is_index, instrument_keys)
  register_adhoc(instrument_key, trading_symbol)


================================================================================
7. TOKEN LIFECYCLE
================================================================================

Components:
  services/token_service.py
    - in-memory token cache
    - refresh_tokens()
    - get_access_token()
    - get_token_document()

  token_tasks/token_store.py
    - MongoDB token document CRUD
    - save_access_token()
    - update_validation_status()
    - get_masked_token_info()
    - never logs/returns raw token

  token_tasks/token_monitor.py
    - check_upstox_token_validity()
    - masks PII
    - sanitizes long token-like error strings
    - updates MongoDB validation status
    - optionally sends Telegram success

  token_tasks/upstox_token_client.py
    - Upstox profile API validation
    - get_profile(), get_funds()
    - validate_token() -> (is_valid, profile, error)

  token_tasks/telegram_token_bot.py
    - long polling worker
    - commands:
        /save_token
        /token_status
        /token_check
        /cancel
        /help
    - restricts to TELEGRAM_CHAT_ID if configured
    - deletes submitted token message when possible
    - validates before saving
    - never echoes raw token

Token flow:

Telegram /save_token
  -> bot sets pending chat
User sends token
  -> bot deletes message
  -> validates length >= 50
  -> upstox_token_client.validate_token()
  -> if valid:
       token_store.save_access_token()
       token_store.update_validation_status()
       token_service.refresh_tokens() later / cache refresh on next read
  -> Telegram confirmation

Scheduled token flow:
  APScheduler token_lifecycle_check
    -> check_upstox_token_validity(send_success_message=False)
    -> token_store.update_validation_status()
    -> Telegram warning/error if missing/invalid


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
    +-- missing -> skipped: first_candle_not_available
    |
    v
  compute_levels(high, low)
    H, L, A, D_H, D_L,
    R1, S1, R2, S2, R3, S3,
    R3_Threshold, S3_Threshold
    |
    v
  write opening_range.json next to ema_crosses.json
    or data/_adhoc/<safe_key>/opening_range.json
    |
    v
  return status ok/skipped/error

API:
  GET  /admin/opening-range
  GET  /admin/opening-range/{instrument_key}
  POST /admin/opening-range/recalculate
  POST /admin/opening-range/recalculate/{instrument_key}


================================================================================
9. SCHEDULER JOBS
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

start() is idempotent.
shutdown() stops scheduler if running.


================================================================================
10. HTTP / API ROUTE MAP
================================================================================

Page / UI:
  GET /                         index.html dashboard
  GET /charts                   instrument search page
  GET /chart/{instrument_key}   chart page
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

Admin:
  POST /admin/hard-refresh
  GET  /admin/backup-status
  GET  /admin/cleanup-status
  POST /admin/cleanup

Folders:
  GET /admin/folders
  GET /admin/folders/{folder_name}
  GET /admin/folders/{folder_name}/download

Opening Range:
  GET  /admin/opening-range
  GET  /admin/opening-range/{instrument_key}
  POST /admin/opening-range/recalculate
  POST /admin/opening-range/recalculate/{instrument_key}

WebSockets:
  /ws/live
  /ws/chart
  /all-feeds
  /ws/all-feeds
  /ws/ema-crosses
  /ws/ema

NOTE:
  README mentions /health and /token/status, but the shown router code does not
  define them. Verify before relying on those paths.


================================================================================
11. DATA / LOG / FILE LAYOUT
================================================================================

Project root:
  main.py
  core/config.py
  core/logger.py
  routes/
  services/
  token_tasks/
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
      live_crosses.json          written by live_cross_store
      opening_range.json         written by opening_range_service
    nifty/<strike>_<TYPE>/
      candles.json
      ema_crosses.json
      live_crosses.json
      opening_range.json
    sensex/<strike>_<TYPE>/
      ...
    _adhoc/<safe_key>/
      opening_range.json         for ad-hoc indices without backup folder

Logs:
  logs/<module>.log              rotating by line count, max 1000 lines, 3 backups
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

Cleanup default:
  remove: data, temp, downloads, output, __pycache__, .pytest_cache,
          htmlcov, test_logs
  preserve: meta_data, logs, logs_text, .git, .venv, venv, myenv,
            templates, routes, services, core, token_tasks, static, tests


================================================================================
12. CONFIGURATION MAP
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


================================================================================
13. FUTURE MODIFICATION GUIDE
================================================================================

Add a new startup / refresh task:
  File: routes/admin_routes.py
  Function: _run_full_refresh_sync()
  Add step in the canonical order, wrap in try/except, add to result summary.

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
  Look for fullFeed, ff, indexFF, marketFF, oc, marketOHLC, I1, vtt.

Change EMA / cross rules:
  Files:
    services/live_ema_engine.py
    core/config.py
  Methods:
    _compute_cross()
    _detect_cross()
    _ema_series(), _atr(), _sma()
  Config:
    EMA_* variables

Change Opening Range formula:
  File: services/opening_range_service.py
  Function: compute_levels()
  Also adjust:
    OPENING_RANGE_* config
    opening_range_routes.py if API shape changes

Change backup extraction:
  File: services/backup_data_service.py
  Methods:
    _blob_doc()
    _metadata_doc()
    _extract_bytes()
    refresh()

Change instrument registry:
  File: services/instrument_registry.py
  Methods:
    load(), get(), resolve(), register_adhoc(), query()

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

Change scheduler jobs:
  File: services/scheduler_service.py
  Method: start()
  Job functions live in token_tasks/.

Change UI:
  Files:
    templates/base.html
    templates/index.html
    templates/instrument_list.html
    templates/chart.html
    templates/show_logs.html

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
14. KNOWN RISKS / GOTCHAS
================================================================================

1. README / architecture.md are outdated.
   Actual code includes EMA, Opening Range, live feed, live backfill,
   live crosses, and related routes.

2. Cleanup removes data/ before backup extraction.
   Startup order is intentional: cleanup -> token -> backup -> registry.
   Do not move backup before cleanup unless you change that policy.

3. Logs are deliberately not cleaned.
   Logger holds open handles into logs/ and logs_text/.
   Removing them on Windows fails; on Linux it can break rotation.

4. live_cross_store.reset_all() clears memory only.
   It does not delete disk live_crosses.json.
   Next save hydrates from disk if the file day matches the cross day.

5. Live feed without token:
   RuntimeError("No Upstox access token available for live feed")
   Feed retries every UPSTOX_WS_RECONNECT_INTERVAL seconds.

6. live_backfill uses Upstox intraday REST per instrument.
   If token missing or Upstox rate-limits, it logs warnings and continues.

7. _run_full_refresh_sync is synchronous and called via asyncio.to_thread.
   Keep CPU/network blocking work inside it or inside services it calls.

8. Manual hard refresh passes loop=None.
   If the app is already running, startup already wired the loop.
   If you change this, pass asyncio.get_running_loop().

9. Port mismatch:
   Procfile uses 8000.
   run.bat and start.sh use 8010.

10. Telegram bot is disabled by default unless TELEGRAM_ENABLED and
    TELEGRAM_TOKEN_BOT_ENABLED are true and credentials are set.

11. Upstox SDK on_close signature historically caused:
    "on_close() takes 0 positional arguments but 2 were given"
    Current code uses on_close(*args, **kwargs). Keep it that way.

12. Orphan routes:
    README mentions /health and /token/status.
    The shown router code does not define them. Verify if needed.


================================================================================
15. QUICK MENTAL MODEL
================================================================================

Startup:
  Clean -> token -> backup -> registry -> wire -> reset crosses ->
  backfill EMA -> calculate OR -> start feed -> scheduler -> telegram.

Live:
  Upstox WS -> extract tick -> EMA engine -> finalise candle ->
  detect cross -> save live_crosses.json -> broadcast crosses.
  Every tick also broadcasts feed snapshots.

APIs:
  UI reads /live/* and /api/upstox/*.
  Admin triggers /admin/hard-refresh.
  Logs at /logs and /ui/logs/*.
  OR at /admin/opening-range/*.

Persistence:
  MongoDB: token + backup archive.
  data/: candles.json, ema_crosses.json, live_crosses.json,
         opening_range.json.
  logs/ + logs_text/: runtime logs.

Rule:
  _run_full_refresh_sync is the single source of truth for startup,
  daily refresh, and manual hard refresh.
  Any new lifecycle task should be added there first.

================================================================================
END OF FILE
================================================================================