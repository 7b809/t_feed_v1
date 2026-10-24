# Market Data Foundation (`market-engine-v1`)

A single-process FastAPI engine that runs the **complete market-data pipeline**
for Upstox:

- **Token lifecycle** — MongoDB token store, Telegram token bot, scheduled validation
- **Backup extraction** — pull the candle-backup archive from MongoDB → `data/`
- **Instrument registry** — scan `data/**/ema_crosses.json` → live registry
- **Live WebSocket feed** — Upstox `MarketDataStreamerV3` with 1-minute candle aggregation
- **EMA cross engine** — 9/21 EMA + ATR + 6 filters, per-instrument cross detection + broadcast
- **Opening Range (OR)** — R1/S1/R2/S2/R3/S3 + thresholds from the 09:15 candle
- **Daily MEGA backup** — zip `data/` at 16:00 IST, upload to MEGA, track state in MongoDB
- **Runtime order toggle** — process-local enable/disable, shared with Telegram bot
- **Live UI** — Index/option-chain dashboard, chart pages, ProChart, multi-chart layout, log viewer

All three refresh triggers — **project startup**, **scheduled daily refresh**,
**manual hard-refresh** — run the exact same task list through the single
entry point `routes.admin_routes._run_full_refresh_sync`.

---

## Table of contents

1. [What's new since the last release](#1-whats-new-since-the-last-release)
2. [Architecture overview](#2-architecture-overview)
3. [Live data pipeline](#3-live-data-pipeline)
4. [Daily MEGA backup](#4-daily-mega-backup)
5. [Scheduler jobs](#5-scheduler-jobs)
6. [Configuration reference](#6-configuration-reference)
7. [Routes & API reference](#7-routes--api-reference)
8. [WebSocket reference](#8-websocket-reference)
9. [Telegram commands & notifications](#9-telegram-commands--notifications)
10. [Data layout](#10-data-layout)
11. [Logging](#11-logging)
12. [Install, run, verify](#12-install-run-verify)
13. [Known limitations & gotchas](#13-known-limitations--gotchas)
14. [File inventory](#14-file-inventory)

---

## 1. What's new since the last release

The engine has been trimmed to the market-data foundation: token lifecycle,
backup extraction, instrument registry, live WebSocket feed, EMA cross
engine, Opening Range levels, order surface, and MEGA-based daily backup.
All strategy pipelines have been removed.

### 1.1 Strategy pipelines — removed

Strategy 1 (buffer filter → isolation → R2 activation → order) and Strategy 2
(S3-touch watcher → next-cross order) have been removed from the codebase.
This includes:

- All `services/strategy_*.py` and `services/strategy_2_*.py` modules
- All `token_tasks/strategy_*.py` and `token_tasks/strategy_2_*.py` wrappers
- All `routes/strategy_routes.py`, `routes/strategy_2_routes.py`,
  `routes/dashboard_routes.py`
- `templates/strategy_dashboard.html`
- Every strategy-related job in `services/scheduler_service.py`
- Strategy stages inside `routes/admin_routes._run_full_refresh_sync`
- Every `STRATEGY_*` and `STRATEGY_2_*` constant in `core/config.py`

The Opening Range service and `/admin/opening-range/*` API are still live —
they are part of the market-data foundation and feed the chart UI.

### 1.2 `TelegramService.send_message()` — signature fix (bug)

`services/telegram_service.py` originally required both `title` **and**
`message`. That crashed every caller that passed a single pre-formatted
`build(...)` string as the title. Fixed by making `message` optional with
default `""`.

```python
def send_message(self, title: str, message: str = "", level: str = "INFO") -> bool:
1.3 Simulation replay — attempted and removed
A "historical replay" feature was built and then removed on request.
Any leftover files from that batch should be deleted:

text
core/market_calendar.py                   (unused — delete)
services/simulation_replay_service.py     (unused — delete)
routes/simulation_routes.py               (unused — delete)
templates/simulation_dashboard.html       (unused — delete)
token_tasks/simulation_task.py            (unused — delete)
And remove the SIMULATION_* block from core/config.py and .env.example
if it's still present.

1.4 Daily MEGA backup (new)
Every day at 16:00 IST (configurable) the pipeline zips each configured
folder (default ["data"]) and uploads the resulting archive to MEGA.
Progress-state documents are tracked in MongoDB — one per date, keyed by
_id = "YYYY-MM-DD":

text
<MEGA_BACKUP_ROOT>/<folder>/<folder>_<YYYY-MM-DD>.zip
e.g. market_engine_backup/data/data_2026-10-20.zip
Full details in §4.

Files added for the daily backup:

text
services/daily_backup_service.py
token_tasks/daily_backup_task.py
Endpoints added:

text
GET  /admin/daily-backup-status
GET  /admin/daily-backup-history
POST /admin/daily-backup-run
1.5 Change summary table
Change	Type	Files touched
Strategy 1 and Strategy 2 removed	Removed	~25 files deleted, references stripped from main.py, admin_routes.py, scheduler_service.py, config.py, config_validation.py, watchdog_task.py, base.html, live_routes.py, page_routes.py, .env.example, docs
Telegram send_message() fix	Bug fix	services/telegram_service.py
Simulation replay (attempted, then removed)	Removed	5 files deleted, references reverted
Daily MEGA backup	New feature	services/daily_backup_service.py, token_tasks/daily_backup_task.py, services/scheduler_service.py, core/config.py, .env.example, routes/admin_routes.py
2. Architecture overview
text
┌───────────────────────────────────────────────────────────────────────────┐
│                            Browser / UI                                   │
│  index.html (option chains) · chart.html · prochart.html · layout.html    │
│  show_logs.html (log viewer)                                             │
└─────────────────────────────────────┬─────────────────────────────────────┘
                                      │ HTTP / WebSocket
                                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                          FastAPI (main.py)                               │
│                                                                           │
│  Routers:                                                                 │
│    page_routes            /, /charts, /chart/{key}, /favicon.ico          │
│    live_routes            /live/*, /ws/live, /ws/chart, /ws/ema*, /all-feeds │
│    admin_routes           /admin/hard-refresh, /admin/cleanup*,           │
│                           /admin/backup-status, /admin/daily-backup-*     │
│    folder_routes          /admin/folders, /admin/folders/{name}/download  │
│    opening_range_routes   /admin/opening-range/*                          │
│    upstox_routes          /api/upstox/*                                   │
│    log_routes             /logs, /ui/logs, /ui/logs/{filename}            │
│    prochart_routes        /prochart, /api/prochart/*                      │
│    layout_routes          /layout_1, /admin/layouts/*                     │
│    daily_backup_routes    /admin/daily-backup-*                           │
│    market_holiday_routes  /admin/market-holiday*                          │
│    order_routes           /admin/orders/*   (buy/sell/exit)               │
│    margin_routes          /admin/orders/margin*                           │
│    readiness_routes       /admin/readiness, /admin/ops/*                  │
└─────────────────────────────────────┬─────────────────────────────────────┘
                                      │
                                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                                Services                                   │
│                                                                           │
│  Token / Telegram                                                         │
│    services/token_service.py           cached Upstox token                │
│    token_tasks/token_store.py          MongoDB token doc                  │
│    token_tasks/token_monitor.py        token validity checks              │
│    token_tasks/upstox_token_client.py  Upstox profile/funds               │
│    services/telegram_service.py        notifications                      │
│    services/telegram_format.py         message formatter                  │
│    token_tasks/telegram_token_bot.py   /save_token, /token_status, etc.   │
│    token_tasks/order_toggle_commands.py /orders_enable/disable/status     │
│                                                                           │
│  Data / Registry                                                          │
│    services/backup_data_service.py     Mongo blob → data/ extraction      │
│    services/instrument_registry.py     scan data/ for ema_crosses.json    │
│    services/cleanup_service.py         delete transient folders           │
│    services/market_holiday_service.py  Upstox holiday check + cache       │
│    services/order_runtime_service.py   runtime order toggle               │
│                                                                           │
│  Live pipeline                                                            │
│    services/live_feed_service.py       Upstox WebSocket                   │
│    services/live_ema_engine.py         1-min candles + EMA cross          │
│    services/live_cross_store.py        live_crosses.json persistence      │
│    services/live_broadcast.py          in-process pub/sub                 │
│    services/live_backfill_service.py   seed EMA from backup + intraday    │
│                                                                           │
│  Opening Range                                                            │
│    services/opening_range_service.py   OR levels + opening_range.json     │
│                                                                           │
│  Order surface (live Upstox orders)                                       │
│    upstox_services/_sanitise.py        shared redaction                   │
│    upstox_services/lot_sizes.py        NIFTY 65, SENSEX 20, …             │
│    upstox_services/place_order.py      OrderApiV3 MARKET buy/sell         │
│    upstox_services/exit_all_positions.py OrderApi.exit_positions()        │
│    upstox_services/margin_check.py     ChargeApi.post_margin              │
│                                                                           │
│  Daily backup                                                             │
│    services/daily_backup_service.py    zip folders → MEGA → track in Mongo│
│                                                                           │
│  Scheduling                                                               │
│    services/scheduler_service.py       APScheduler singleton              │
└─────────────────────────────────────┬─────────────────────────────────────┘
                                      │
                                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                            External Systems                               │
│                                                                           │
│  MongoDB:                                                                 │
│    MONGO_DB.TOKENS_COLLECTION          Upstox token document              │
│    MONGO_DB.BACKUP_METADATA_COLLECTION candle-backup metadata             │
│    MONGO_DB.BACKUP_BLOB_COLLECTION     candle-backup archive blob         │
│    DAILY_BACKUP_MONGO_DB.<coll>        daily data backup progress state   │
│    PROCHART_MONGO_DB.<coll>            ProChart settings + alerts         │
│                                                                           │
│  Upstox:                                                                  │
│    REST: profile, search, historical, intraday                            │
│    REST: MarketHolidaysAndTimingsApi.get_holiday                          │
│    REST: OrderApiV3.place_order, OrderApi.exit_positions                  │
│    REST: ChargeApi.post_margin                                            │
│    WebSocket: MarketDataStreamerV3                                        │
│                                                                           │
│  Telegram:                                                                │
│    notifications + token management bot + order toggle commands           │
│                                                                           │
│  MEGA:                                                                    │
│    Daily data archive destination                                         │
└───────────────────────────────────────────────────────────────────────────┘
The single source of truth: _run_full_refresh_sync
text
uvicorn main:app
  │
  ▼
app_lifespan (main.py)
  │
  ▼
asyncio.to_thread(_run_full_refresh_sync, "startup", loop)
  │
  ├─  1.  Telegram start
  ├─  2.  cleanup_service.run()             → removes data/, temp/, downloads/, output/, __pycache__/, ...
  ├─  3.  token_service.refresh_tokens()    → Mongo token → memory
  ├─  4.  check_upstox_token_validity()     → Upstox profile API
  ├─  5.  backup_data_service.refresh()     → Mongo blob → data/
  ├─  6.  instrument_registry.load()        → scan data/**/ema_crosses.json
  ├─  6b. pre-register core indexes         → NIFTY 50, SENSEX (ad-hoc)
  ├─  7.  wire loop + cross handler         → broadcast + live_cross_store
  ├─  8.  live_cross_store.reset_all()      → in-memory only
  ├─  9.  live_backfill_service.seed_all()  → backup + intraday → EMA engine
  ├─ 10.  opening_range_service.calculate_all()
  ├─ 11.  live_feed_service.start/restart()
  ├─ 12.  scheduler_service.start()
  ├─ 13.  telegram_token_bot.start()
  ├─ 14.  Telegram completion
  └─ 15.  return result dict
main.py mounts every router and shuts down every background component
in the lifespan finally block.

3. Live data pipeline
text
Upstox WebSocket (MarketDataStreamerV3)
  │
  ▼
LiveFeedService._handle_feed(payload)
  │  for each instrument_key:
  ├─ _extract_tick(feed) → { ltp, prev_close, volume, candle_1m (interval "I1") }
  │
  ├─► _latest_ltp[key] = ltp    ← tracker thread polls this
  │
  ├─► live_ema_engine.on_tick() ← candle aggregation + cross detect
  │       │
  │       └─► _ingest → forming candle → minute rollover → _finalise()
  │               └─► _detect_cross → 6 filters → score/valid
  │                       └─► _cross_handler(entry, cross)
  │                               ├─► live_cross_store.save() → live_crosses.json
  │                               └─► live_broadcast.publish_sync("crosses", ...)
  │
  └─► live_broadcast.publish_sync("feed", ...) → /ws/live, /ws/chart, /all-feeds
_extract_tick search order: fullFeed → ff → indexFF / marketFF /
oc / marketLevel. Extracts ltpc.ltp for LTP, marketOHLC.ohlc for
the I1 candle, eFeedDetails.vtt for volume.

EMA engine internals: per-instrument _forming candle, _candles
rolling (max 500), minute rollover → finalize → append → _detect_cross()
→ _compute_cross(). Six filters: trend, separation (vs ATR),
slope, volume, body, distance (vs ATR).
valid = score >= EMA_MIN_VALIDATION_SCORE (default 3/6).

Backfill at startup — live_backfill_service.seed_all() reads each
instrument's backup candles.json tail + today's Upstox intraday 1-min
candles, merges by minute, seeds the EMA engine, and scans the seeded
series for crosses that occurred after today 00:00 (so mid-day restarts
do not lose earlier crosses and do not need 20+ minutes to warm up).

4. Daily MEGA backup
A service that runs every day at 16:00 IST (configurable) and zips each
configured folder (default ["data"]) into its own archive. The archive is
uploaded to MEGA under:

text
<MEGA_BACKUP_ROOT>/<folder>/<folder>_<YYYY-MM-DD>.zip
e.g. market_engine_backup/data/data_2026-10-20.zip
Destination
Database: UPSTOX_APP_V2 (via DAILY_BACKUP_MONGO_DB)

Collection: daily_backup_state (via DAILY_BACKUP_STATE_COLLECTION)

Doc _id: "YYYY-MM-DD" — one document per date, upserted on every run

NOT BACKUP_BLOB_COLLECTION — that collection remains reserved for the
candle-backup extract that is re-consumed on every refresh

Behaviour matrix
Scenario	Outcome
Normal — every folder zips and uploads	status = "uploaded"
Some folders uploaded, others failed	status = "partial"
All folders failed	status = "failed"
No source folder produced a zip	status = "no_data"
Feature disabled (DAILY_BACKUP_ENABLED=false)	{"status":"disabled"} returned, no doc written
DAILY_BACKUP_FOLDERS empty	{"status":"no_folders"} returned, no doc written
Manual run while scheduled run is active	{"status":"already_running"} returned, no doc written
MEGA login fails	status = "failed", error captured
MongoDB unreachable for state writes	run still completes; state write logged
Document schema
javascript
{
  _id:              "2026-10-20",
  date:             "2026-10-20",
  status:           "uploaded" | "partial" | "failed" | "no_data",
  run_reason:       "scheduled" | "manual",
  root_folder:      "market_engine_backup",
  folders:          ["data"],
  files: [
    {
      folder:       "data",
      filename:     "data_2026-10-20.zip",
      size_bytes:   8806042,
      size_human:   "8.4 MB",
      mega_link:    "https://mega.nz/file/...",
      uploaded:     true,
      error:        null,
      remote_path:  "market_engine_backup/data/data_2026-10-20.zip",
      file_count:   3412,
    }
  ],
  total_size_bytes: 8806042,
  total_size_human: "8.4 MB",
  updated_at:       ISODate(...),
  updated_at_ist:   "2026-10-20 16:00:22 IST",
  error:            null,
}
Design notes
The zip is built in a temp file on disk, never fully in memory.

Temp files are always deleted, even on failure.

A non-blocking lock prevents scheduled and manual runs from overlapping.

Symlinked subdirectories are not followed.

Folder paths that escape the project root are refused.

The service never raises — always returns a summary dict.

Endpoints
text
GET  /admin/daily-backup-status        → last run summary + last N days of state
GET  /admin/daily-backup-history       → only the date-keyed history
POST /admin/daily-backup-run           → trigger now (returns summary dict)
Manual verification
bash
curl -X POST http://localhost:8010/admin/daily-backup-run
curl http://localhost:8010/admin/daily-backup-status

# Read back from MongoDB (data field excluded for brevity)
mongosh "mongodb+srv://..." --eval '
  db.getSiblingDB("UPSTOX_APP_V2")
    .daily_backup_state
    .find({}, {files: 1, status: 1, updated_at_ist: 1})
    .sort({_id: -1})
    .limit(7)
'
Note on data/ being wiped by the morning refresh
The daily refresh calls cleanup_service.run() which deletes data/ before
re-extracting the candle-backup archive. The daily MEGA backup is therefore a
point-in-time snapshot (yesterday's full strategy output at 16:00). It is not
a live mirror. If you need additional folders preserved across the morning
cleanup, add them to CLEANUP_RESERVED_FOLDERS.

5. Scheduler jobs
services/scheduler_service.py::SchedulerService.start() is
idempotent — safe to call multiple times.

Job id	When	Function
token_lifecycle_check	every UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES (default 30)	check_upstox_token_validity
daily_refresh	DAILY_REFRESH_DAYS at DAILY_REFRESH_HOUR:MINUTE (default Mon–Fri 08:30)	run_daily_refresh → _run_full_refresh_sync("daily")
opening_range_calc	DAILY_REFRESH_DAYS at OPENING_RANGE_HOUR:MINUTE:SECOND (default 09:16:10)	run_opening_range_calculation
readiness_digest	weekdays at 08:45	run_readiness_digest
preflight_recheck	weekdays at 09:05	run_preflight_recheck
or_watchdog	OR + 60 s (default 09:17:10)	run_or_watchdog
daily_backup	DAILY_BACKUP_DAYS at DAILY_BACKUP_HOUR:MINUTE (default every day 16:00)	run_daily_backup
6. Configuration reference
All flags are read from .env at startup via core/config.py.
Toggle behaviour by editing .env — no code change needed.

Core / Mongo / Telegram
Variable	Default	Purpose
MONGO_URL	—	MongoDB connection string
MONGO_DB	—	Database containing token doc + candle-backup archive
TOKENS_COLLECTION	—	Collection containing the token document
UPSTOX_TOKEN_DOC_ID	upstox_access_token	Token document _id
UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES	30	Scheduled token validation interval
MARKET_TIMEZONE	Asia/Kolkata	Timezone for all IST operations
TELEGRAM_ENABLED	false	Enables Telegram notifications & polling
TELEGRAM_BOT_TOKEN	—	Telegram bot credential
TELEGRAM_CHAT_ID	—	Authorized chat
TELEGRAM_TIMEOUT_SECONDS	10	HTTP timeout for Telegram API
TELEGRAM_TOKEN_BOT_ENABLED	true	Enables token polling worker
TELEGRAM_TOKEN_BOT_POLL_SECONDS	3	Polling retry delay
TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT	20	Telegram long-poll timeout
TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT	true	Restrict commands to TELEGRAM_CHAT_ID
Charts / backup extraction / live feed
Variable	Default	Purpose
CHART_MAX_BATCH_WORKERS	8	Max parallel history requests
CHART_LIVE_POLL_SECONDS	30	Chart WS refresh interval (min 15s)
BACKUP_METADATA_COLLECTION	app_tokens	MongoDB collection for candle-backup metadata
BACKUP_METADATA_DOC_ID	backup_candles	Metadata doc _id
BACKUP_BLOB_COLLECTION	backup_collection	MongoDB collection for candle-backup archive
BACKUP_BLOB_DOC_ID	backup_historical_candles	Blob doc _id
LIVE_DATA_DIR	data	Local extraction target
BACKUP_MAX_AGE_MINUTES	0	0 = always re-extract
UPSTOX_WS_MODE	full	full / ltpc / full_d30 / option_greeks
UPSTOX_WS_AUTO_START	true	Auto-start the live feed
UPSTOX_WS_RECONNECT_INTERVAL	10	Reconnect delay (s)
UPSTOX_WS_MAX_RETRIES	0	0 = infinite
HEALTH_FEED_LOG	true	Enable periodic health-feed log
HEALTH_FEED_INSTRUMENT	NSE_INDEX|Nifty 50	Instrument to log
HEALTH_FEED_INSTRUMENT_INTERVAL	5	Interval in minutes
EMA engine
Variable	Default	Purpose
EMA_FAST_PERIOD	9	Fast EMA
EMA_SLOW_PERIOD	21	Slow EMA
EMA_ATR_PERIOD	14	ATR period
EMA_VOLUME_LOOKBACK	20	Volume SMA window
EMA_SLOPE_LOOKBACK	3	Slope comparison window
EMA_SEP_ATR_MIN	0.15	Minimum EMA separation vs ATR
EMA_VOLUME_MULT	1.0	Volume threshold multiplier
EMA_DISTANCE_ATR_MAX	1.5	Max distance from slow EMA vs ATR
EMA_MIN_VALIDATION_SCORE	3	Min filter score to be valid
EMA_ENABLE_VALIDATION	true	If false, every cross is valid
Scheduler / OR
Variable	Default	Purpose
DAILY_REFRESH_ENABLED	true	Enable daily refresh
DAILY_REFRESH_HOUR	8	Daily refresh hour
DAILY_REFRESH_MINUTE	30	Daily refresh minute
DAILY_REFRESH_DAYS	mon,tue,wed,thu,fri	Cron day-of-week
VERBOSE_LOG	false	When true, dump per-instrument decision detail
OPENING_RANGE_FILENAME	opening_range.json	Output filename
OPENING_RANGE_FIRST_MINUTE	09:15	First 1-min candle
OPENING_RANGE_HOUR	9	OR calc trigger hour
OPENING_RANGE_MINUTE	16	OR calc trigger minute
OPENING_RANGE_SECOND	10	OR calc trigger second
OPENING_RANGE_REST_FALLBACK_ENABLED	true	REST fallback for late 09:15 candle
OPENING_RANGE_REST_TIMEOUT_SECONDS	15	REST fallback timeout
Daily backup
Variable	Default	Purpose
DAILY_BACKUP_ENABLED	true	Master switch
DAILY_BACKUP_HOUR	16	Scheduler hour
DAILY_BACKUP_MINUTE	0	Scheduler minute
DAILY_BACKUP_DAYS	mon,tue,wed,thu,fri,sat,sun	Cron day-of-week
DAILY_BACKUP_FOLDERS	data	Folders to zip (relative to project root)
MEGA_ENABLED	true	MEGA upload toggle
MEGA_EMAIL	—	MEGA account email
MEGA_PASSWORD	—	MEGA account password
MEGA_BACKUP_ROOT	market_engine_backup	Remote root under MEGA
DAILY_BACKUP_MONGO_DB	UPSTOX_APP_V2	Destination database
DAILY_BACKUP_STATE_COLLECTION	daily_backup_state	Destination collection
DAILY_BACKUP_STATE_DAYS	7	Number of history days returned
DAILY_BACKUP_SOURCE_LABEL	project dir name	Value written to the folder field
ProChart
Variable	Default	Purpose
PROCHART_MONGO_DB	UPSTOX_APP_V2	Destination database
PROCHART_SETTINGS_COLLECTION	app_tokens	Settings collection
PROCHART_SETTINGS_DOC_ID	prochart_settings	Settings doc _id
PROCHART_ALERTS_COLLECTION	prochart_alerts	Alerts collection
PROCHART_ALERTS_DATA_SUBDIR	prochart_alerts	Disk mirror subfolder
PROCHART_DEFAULT_TRIGGER	after_r3_or_s3_touch	Default alert trigger
PROCHART_DEFAULT_INSTRUMENT_KEY	NSE_INDEX|Nifty 50	Default chart instrument
PROCHART_URL_OVERRIDE_PERSISTS	true	Persist URL params as new defaults
7. Routes & API reference
Pages / UI
Method	Path	Purpose
GET	/	Multi-index dashboard + option chains
GET	/charts	Instrument search page
GET	/chart/{instrument_key}	Candlestick chart
GET	/prochart	ProChart page
GET	/layout_1	Multi-chart layout page
GET	/logs	HTML log viewer
GET	/favicon.ico	204
Logs
Method	Path	Purpose
GET	/ui/logs	JSON list of log files
GET	/ui/logs/{filename}	Raw text of one log file
Upstox proxy
Method	Path	Purpose
GET	/api/upstox/token	Masked token info
GET	/api/upstox/search	Instrument search
GET	/api/upstox/instrument	Lookup by key
GET	/api/upstox/intraday	Intraday 1-min
GET	/api/upstox/historical	Historical 1-min
GET	/api/upstox/subscription-status	Registry check
POST	/api/upstox/subscribe	On-demand subscribe
Live REST
Method	Path	Purpose
GET	/live/status	Feed status + instrument count
GET	/live/instruments	Registry listing
GET	/live/instruments/resolve	Key or triple resolver
GET	/live/crosses	Crosses for one or all instruments
GET	/live/crosses/{instrument_key}	Path form
GET	/live/ema	Current EMA snapshot
GET	/live/ema/{instrument_key}	Path form
GET	/live/ema-series	EMA series per minute
GET	/live/candles	Rolling 1-min candles
GET	/live/snapshot	Feed + EMA in one call
GET	/live/snapshot/{instrument_key}	Path form
Single-instrument endpoints accept either instrument_key
or underlying + strike + type. Values are normalized
("nifty", "NIFTY 50", "nifty50" all resolve).

Admin — core
Method	Path	Purpose
POST	/admin/hard-refresh	Run the unified refresh (same as startup)
GET	/admin/backup-status	Last candle-backup extraction summary
GET	/admin/cleanup-status	Last cleanup summary
POST	/admin/cleanup	Run cleanup standalone
GET	/admin/daily-backup-status	Last daily-backup run summary + history
GET	/admin/daily-backup-history	Date-keyed history only
POST	/admin/daily-backup-run	Trigger the daily backup now
Admin — folders
Method	Path	Purpose
GET	/admin/folders	List exposable folders
GET	/admin/folders/{folder_name}	One folder's metadata
GET	/admin/folders/{folder_name}/download	Stream folder as .zip
Admin — Opening Range
Method	Path	Purpose
GET	/admin/opening-range	All OR payloads
GET	/admin/opening-range/{instrument_key}	One instrument
POST	/admin/opening-range/recalculate	Force full recalc
POST	/admin/opening-range/recalculate/{instrument_key}	Force one
Admin — Market holiday
Method	Path	Purpose
GET	/admin/market-holiday	Today's holiday payload
GET	/admin/market-holiday/{date_str}	Holiday payload for a date
GET	/admin/market-holiday/cache-status	Cache diagnostics
POST	/admin/market-holiday/clear-cache	Clear the in-process cache
Admin — Orders
Method	Path	Purpose
POST	/admin/orders/buy	Place a BUY order (lot-based)
POST	/admin/orders/sell	Place a SELL order (lot-based)
POST	/admin/orders/exit-all	Exit every open position
GET	/admin/orders/runtime-status	Current toggle state
POST	/admin/orders/runtime-enable	Enable orders
POST	/admin/orders/runtime-disable	Disable orders
Admin — Margin
Method	Path	Purpose
POST	/admin/orders/margin	Check margin for one instrument
POST	/admin/orders/margin/batch	Check margin for several instruments
Admin — Readiness / ops
Method	Path	Purpose
GET	/admin/readiness	Last readiness snapshot
POST	/admin/readiness	Force a fresh readiness check
GET	/admin/readiness/history	7-day health-log history
GET	/admin/ops/runbook	Operator cheat sheet as JSON
POST	/admin/ops/warmup	Backfill + registry only
POST	/admin/ops/smoke-test	5-probe smoke test (no side effects)
POST	/admin/ops/resubscribe-feed	Force live-feed reconnect
POST	/admin/ops/revalidate-token	Force token re-check
Admin — ProChart
Method	Path	Purpose
GET	/api/prochart/settings	Current settings
POST	/api/prochart/settings	Save settings (partial merge)
POST	/api/prochart/alert	Save + buffer an alert
GET	/api/prochart/alerts	Last 100 alerts (in-memory)
GET	/api/prochart/alerts/{instrument_key}	Persisted alerts for an instrument
Admin — Layouts
Method	Path	Purpose
GET	/admin/layouts	List layouts
POST	/admin/layouts	Create layout
GET	/admin/layouts/{layout_id}	One layout
PATCH	/admin/layouts/{layout_id}	Update one layout
DELETE	/admin/layouts/{layout_id}	Delete one layout
8. WebSocket reference
Path	Topic	Purpose
/ws/live	feed	Live tick snapshots, filtered
/ws/chart	feed	Chart feed snapshots
/all-feeds (alias /ws/all-feeds)	feed	All-instrument feed
/ws/ema-crosses	crosses	EMA cross events
/ws/ema	ema (poll)	Streaming EMA snapshot per instrument
Clients can send {"action":"subscribe","instrument_key":"..."} on
the feed sockets to add subscriptions on the fly.

9. Telegram commands & notifications
Bot commands (authorized chat only)
Command	Purpose
/save_token	Start token entry — next private message is validated and saved
/token_status	Masked token metadata
/token_check	Validate the stored token
/orders_status	Show current order enable/disable state
/orders_enable	Enable live order placement
/orders_disable	Disable live order placement
/cancel	Cancel pending token entry
/help	List commands
The bot deletes the submitted token message, validates it against
Upstox before saving, and never echoes the raw value.

Notification style
All pipeline messages (startup, shutdown, OR calc, daily backup, token
lifecycle) are sent as emoji + divider messages that render identically
across Telegram clients (no markdown). The shared helper is
services/telegram_format.build(...).

10. Data layout
Extraction target (LIVE_DATA_DIR, default data/)
text
data/
├── index/<underlying>/
│   ├── candles.json
│   ├── ema_crosses.json
│   ├── live_crosses.json        (written by live_cross_store)
│   └── opening_range.json       (written by opening_range_service)
├── nifty/<strike>_<TYPE>/
│   └── (same 4 files)
├── sensex/<strike>_<TYPE>/
│   └── (same 4 files)
├── _adhoc/<safe_key>/opening_range.json   (ad-hoc indices only)
└── prochart_alerts/<safe_key>.json
Logs
logs/<module>.log — rotating by line count, max 1000 lines, 3 backups

logs_text/<module>.txt — append-only plain text mirror

Deliberately not cleaned by cleanup_service — the logger holds
open file handles into both directories at import time; removing them
mid-process breaks rotation on Linux and fails outright on Windows.

Meta backups
meta_data/ — preserved across all refreshes. update_project.py
writes a logs_<YYYYMMDD_HHMMSS>.zip into it before each git update.

11. Logging
Per-module loggers via core.logger.get_logger(__file__):

Console handler (all levels).

LineCountRotatingHandler → logs/<module>.log (rotate at 1000 lines, 3 backups).

PlainTextHandler → logs_text/<module>.txt (append-only).

Important modules:

text
main
admin_routes
live_feed_service
live_backfill_service
live_ema_engine
opening_range_service
daily_backup_service
token_monitor
token_store
upstox_token_client
telegram_token_bot
Set VERBOSE_LOG=true in .env to switch pipeline logs from "lifecycle
only" to full per-instrument decision dumps.

12. Install, run, verify
Install
powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
Then edit .env with real credentials.

Run
powershell
python -m uvicorn main:app --host 0.0.0.0 --port 8000
Windows helpers: run.bat (creates/activates myenv if needed, launches on 8010).

Linux helpers: start.sh (creates/activates myenv, installs requirements,
launches on 8010 with nohup + PID file), stop.sh,
update_project.py (backup logs → stop → git reset/clean/pull →
restart), start_all_projects.sh (boot driver for multiple projects).

Port note: Procfile uses 8000; run.bat and start.sh use
8010. Whichever launcher you use, that's the port.

Verify
With MongoDB and Upstox configured:

GET / — dashboard renders.

GET /charts — search works.

GET /chart/<URL-encoded-instrument-key> — chart renders.

GET /prochart — ProChart page renders.

GET /layout_1 — multi-chart layout renders.

GET /api/upstox/token — returns {"available": true, "masked": "..."}.

GET /live/status — feed reports running: true after startup.

GET /admin/backup-status — last candle-backup extraction summary.

GET /admin/opening-range — OR levels present for at least the core indexes.

POST /admin/hard-refresh — full pipeline re-runs; completion message
arrives in Telegram.

POST /admin/daily-backup-run — returns the backup summary and writes
the doc to UPSTOX_APP_V2.daily_backup_state.

GET /admin/orders/runtime-status — shows the current toggle state.

Telegram: /token_status reports masked metadata; /save_token
accepts a valid token and rejects invalid ones.

Regression tests (mock MongoDB, Upstox, Telegram):

powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
13. Known limitations & gotchas
TelegramService.send_message() signature must stay message: str = "".
Every S1 stage wrapper and the daily backup task call it as
send_message(build(...), level=...). If message becomes required
again, the strategies-adjacent modules crash.

Cleanup wipes data/ before backup extraction. Startup order is
intentional: cleanup → token → backup → registry. Do not reorder unless
you also change the cleanup policy.

Logs are deliberately not cleaned — the logger holds open handles
into logs/ and logs_text/.

The daily backup is a point-in-time snapshot. data/ is wiped at
every weekday refresh by cleanup, so the 16:00 backup captures the
previous day's full output. It is not a live mirror.

DAILY_BACKUP_MAX_BLOB_MB is deprecated and ignored. The MEGA
implementation has no inline size cap. The variable is kept only so an
existing .env that still sets it does not crash the loader.

live_cross_store.reset_all() clears memory only. Disk
live_crosses.json persists; the next save() hydrates from disk if the
file's day matches the incoming cross.

Manual hard refresh passes loop=None. Startup has already wired
the loop; the manual path reuses it. If you change this, pass
asyncio.get_running_loop().

Port mismatch: Procfile = 8000, run.bat / start.sh = 8010.

Upstox SDK on_close signature. Use on_close(*args, **kwargs) —
the historical on_close() (no args) crashes on close.

Order runtime toggle resets to ENABLED on restart by design. If
you need persistence across restarts, move the flag into MongoDB
(services/order_runtime_service.py is the only file to change).

place_order and margin_check never guess the lot size blindly.
The resolution chain (explicit underlying → registry → key scan →
exchange default → unit fallback) is deterministic. If a future
instrument has a new lot size, add it to
upstox_services/lot_sizes.LOT_SIZES; do not let unit_fallback
reach Upstox, because Upstox will reject qty=1 for F&O.

No token or account identifier appears in any log line or API
response. Do not add one — the sanitiser only redacts what it
recognises; if you introduce a new credential field, add its key to
upstox_services/_sanitise.SECRET_KEYS.

Market holiday gate treats provider errors as "not a holiday".
This is intentional: a network outage must never silently skip a real
trading day.

14. File inventory
text
market-engine-v1/
├── main.py                                     FastAPI entry point + lifespan
├── Procfile                                    launch config (port 8000)
├── run.bat                                     Windows launcher (port 8010)
├── start.sh / stop.sh                          Linux launcher (port 8010)
├── update_project.py                           git pull + restart helper
├── start_all_projects.sh                       multi-project boot driver
├── requirements.txt / requirements-dev.txt
├── README.md                                   this file
├── architecture.md                             deeper component map
├── .env.example                                full config template
├── core/
│   ├── config.py                               all env-driven settings
│   └── logger.py                               rotating .log + append-only .txt
├── routes/
│   ├── admin_routes.py                         _run_full_refresh_sync + admin endpoints
│   ├── daily_backup_routes.py                  /admin/daily-backup-*
│   ├── dashboard_routes.py                     (dashboard data — kept for other UIs)
│   ├── folder_routes.py                        /admin/folders/*  (zip download)
│   ├── layout_routes.py                        /layout_1, /admin/layouts/*
│   ├── live_routes.py                          /live/*, /ws/*
│   ├── log_routes.py                           /logs, /ui/logs/*
│   ├── margin_routes.py                        /admin/orders/margin*
│   ├── market_holiday_routes.py                /admin/market-holiday*
│   ├── opening_range_routes.py                 /admin/opening-range/*
│   ├── order_routes.py                         /admin/orders/*
│   ├── page_routes.py                          /, /charts, /chart/{key}
│   ├── prochart_routes.py                      /prochart, /api/prochart/*
│   ├── readiness_routes.py                     /admin/readiness, /admin/ops/*
│   └── upstox_routes.py                        /api/upstox/*
├── services/
│   ├── backup_data_service.py                  candle-backup Mongo blob → data/
│   ├── cleanup_service.py                      transient folder cleanup
│   ├── config_validation.py                    startup config sanity check
│   ├── daily_backup_service.py                 ★ daily MEGA backup
│   ├── instrument_registry.py                  scan data/**/ema_crosses.json
│   ├── layout_service.py                       ★ multi-chart layout persistence
│   ├── live_backfill_service.py                seed EMA engine
│   ├── live_broadcast.py                       in-process pub/sub
│   ├── live_cross_store.py                     live_crosses.json persistence
│   ├── live_ema_engine.py                      1-min candles + EMA cross
│   ├── live_feed_service.py                    Upstox WebSocket
│   ├── market_holiday_service.py               Upstox holiday check + cache
│   ├── normalization.py                        selector normalization
│   ├── opening_range_service.py                OR levels + opening_range.json
│   ├── order_runtime_service.py                runtime order toggle
│   ├── prochart_alerts_service.py              ProChart alert persistence
│   ├── prochart_settings_service.py            ProChart settings persistence
│   ├── readiness_service.py                    aggregate readiness engine
│   ├── scheduler_service.py                    APScheduler singleton
│   ├── telegram_format.py                      emoji/divider formatter
│   ├── telegram_service.py                     Telegram HTTP client
│   └── token_service.py                        cached Upstox token
├── token_tasks/
│   ├── daily_backup_task.py                    scheduler wrapper for daily backup
│   ├── daily_refresh.py                        scheduler wrapper for refresh
│   ├── opening_range_task.py                   scheduler wrapper for OR
│   ├── order_toggle_commands.py                Telegram /orders* commands
│   ├── readiness_digest_task.py                08:45 digest + 09:05 preflight
│   ├── telegram_token_bot.py                   Telegram long-polling bot
│   ├── token_monitor.py                        token validity checks
│   ├── token_store.py                          MongoDB token CRUD
│   ├── upstox_token_client.py                  Upstox profile/funds API
│   └── watchdog_task.py                        OR watchdog
├── templates/
│   ├── base.html                               shared nav + CSS
│   ├── chart.html                              candlestick + EMA + OR levels
│   ├── index.html                              multi-index dashboard + option chain
│   ├── instrument_list.html                    Upstox instrument search
│   ├── layout.html                             ★ multi-chart layout
│   ├── partials/
│   │   └── layout_pane.html                    ★ single layout pane partial
│   ├── prochart.html                           ★ ProChart page
│   └── show_logs.html                          log viewer
├── upstox_services/
│   ├── __init__.py
│   ├── _sanitise.py                            shared credential redaction
│   ├── exit_all_positions.py                   OrderApi.exit_positions()
│   ├── lot_sizes.py                            per-instrument lot-size table
│   ├── margin_check.py                         ChargeApi.post_margin
│   └── place_order.py                          OrderApiV3 buy/sell
├── logs/                                       rotating .log files
├── logs_text/                                  append-only .txt files
├── meta_data/                                  log backups (preserved across refresh)
└── data/                                       extracted backup + OR + crosses
★ = added since the initial Strategy-1-only release.
Reference docs
architecture.md — deeper component map, startup flow diagram, live data
pipeline, scheduler jobs, future modification guide

.env.example — every supported variable with defaults

text

**Changes made in this file:**

**Removed sections:**
- §3 Strategy 1
- §4 Strategy 2
- All references to strategies in the pipeline order, scheduler jobs, config table, route map, data layout, file inventory, and gotchas

**Kept/renumbered sections:**
- §1 What's new (rewritten to describe the strategy removal + retained features)
- §2 Architecture (rewritten diagram — no strategy boxes, no strategy stages in the refresh flow)
- §3 Live data pipeline (was §5)
- §4 Daily MEGA backup (was §6, rewritten for MEGA instead of inline Mongo)
- §5 Scheduler jobs (was §7, trimmed to 7 jobs)
- §6 Configuration reference (was §8, strategy tables removed, `STRATEGY_VERBOSE_LOG` renamed to `VERBOSE_LOG`)
- §7 Routes & API reference (was §9, `/admin/strategy*`, `/dashboard`, `/ui/dashboard/strategy`, `/isolated-dashboard` removed; order/margin/readiness/prochart/layout routes added for accuracy)
- §8 WebSocket reference (was §10)
- §9 Telegram commands (was §11, `/orders_*` commands added)
- §10 Data layout (was §12, `strategy/` and `strategy2/` subfolders removed)
- §11 Logging (was §13, strategy modules removed from "Important modules")
- §12 Install, run, verify (was §14, `/admin/strategy*`, `/admin/strategy2*` verification steps replaced with retained routes)
- §13 Known limitations & gotchas (was §15, strategy-specific gotchas removed, remaining ones renumbered)
- §14 File inventory (was §16, every strategy file removed, order/margin/readiness/prochart/layout/market_holiday files added for accuracy)

**Renamed/reworded:**
- "Two trading strategies — Strategy 1 + Strategy 2" → removed from feature list
- "Daily MongoDB backup — zip data/ and store inline" → "Daily MEGA backup — zip data/, upload to MEGA, track in MongoDB"
- `STRATEGY_VERBOSE_LOG` → `VERBOSE_LOG`
- `GET /dashboard` (strategy dashboard) — removed from routes
- `GET /isolated-dashboard` — removed from routes
- `/admin/strategy/*`, `/admin/strategy2/*`, `/ui/dashboard/strategy` — removed from routes

**Added for accuracy** (routes that exist but were not listed in the original README):
- `POST /admin/orders/buy`, `/sell`, `/exit-all`, `/runtime-*`
- `POST /admin/orders/margin`, `/margin/batch`
- `GET /admin/readiness`, `POST /admin/readiness`, `GET /admin/readiness/history`
- `GET /admin/ops/runbook`, `POST /admin/ops/warmup`, `/smoke-test`, `/resubscribe-feed`, `/revalidate-token`
- `GET /prochart`, `/api/prochart/settings`, `/api/prochart/alert`, `/api/prochart/alerts`
- `GET /layout_1`, `/admin/layouts*`
- `GET /admin/market-holiday*`
- `GET /admin/daily-backup-history`
