# Market Data Foundation (`market-engine-v1`)

A single-process FastAPI engine that runs the **complete market-data +
trading pipeline** for Upstox:

- **Token lifecycle** — MongoDB token store, Telegram token bot, scheduled validation
- **Backup extraction** — pull the candle-backup archive from MongoDB → `data/`
- **Instrument registry** — scan `data/**/ema_crosses.json` → live registry
- **Live WebSocket feed** — Upstox `MarketDataStreamerV3` with 1-minute candle aggregation
- **EMA cross engine** — 9/21 EMA + ATR + 6 filters, per-instrument cross detection + broadcast
- **Opening Range (OR)** — R1/S1/R2/S2/R3/S3 + thresholds from the 09:15 candle
- **Two trading strategies** — Strategy 1 (highly-active days) + Strategy 2 (fallback)
- **Daily MongoDB backup** — zip `data/` (configurable folder list) at 16:00 IST and store it inline
- **Live UI** — Index/option-chain dashboard, chart pages, strategy dashboard, log viewer

All three refresh triggers — **project startup**, **scheduled daily refresh**,
**manual hard-refresh** — run the exact same task list through the single
entry point `routes.admin_routes._run_full_refresh_sync`.

---

## Table of contents

1. [What's new since the last release](#1-whats-new-since-the-last-release)
2. [Architecture overview](#2-architecture-overview)
3. [Strategy 1 — Highly active day strategy](#3-strategy-1--highly-active-day-strategy)
4. [Strategy 2 — Normal day fallback strategy](#4-strategy-2--normal-day-fallback-strategy)
5. [Live data pipeline](#5-live-data-pipeline)
6. [Daily MongoDB backup](#6-daily-mongodb-backup)
7. [Scheduler jobs](#7-scheduler-jobs)
8. [Configuration reference](#8-configuration-reference)
9. [Routes & API reference](#9-routes--api-reference)
10. [WebSocket reference](#10-websocket-reference)
11. [Telegram commands & notifications](#11-telegram-commands--notifications)
12. [Data layout](#12-data-layout)
13. [Logging](#13-logging)
14. [Install, run, verify](#14-install-run-verify)
15. [Known limitations & gotchas](#15-known-limitations--gotchas)
16. [File inventory](#16-file-inventory)

---

## 1. What's new since the last release

The engine was initially a **Strategy-1-only** pipeline. It has since grown
into a **dual-strategy** engine and gained a **daily backup** feature.

### 1.1 Strategy 2 — fallback pipeline (new)

A second strategy that runs on **normal / range-bound days**, i.e. when
Strategy 1's R2 breakout does not happen. Strategy 2 **reuses Stage 1
and Stage 2 of Strategy 1 unchanged** and only replaces Stage 3 + Stage 4:

| Stage 3 (S2) | Watch each Stage-2 isolated contract's **own** isolation level (**S3** by default), record the first touch, then filter to one contract by ticker priority + nearest ATM |
| Stage 4 (S2) | On the **next EMA cross after the S3 touch**, place a paper order — bullish → same type; bearish → **opposite** type at the same strike |

Full details in [§4](#4-strategy-2--normal-day-fallback-strategy).

Files added for Strategy 2:
services/strategy_2_isolation_service.py
services/strategy_2_live_tracker_service.py
services/strategy_2_order_service.py
routes/strategy_2_routes.py
token_tasks/strategy_2_isolation_task.py
token_tasks/strategy_2_order_task.py
services/telegram_format.py

text

### 1.2 Nicer Telegram messages (new)

Every strategy stage and the startup/shutdown lifecycle now send **emoji-rich,
divider-lined** Telegram messages (pure Unicode, no markdown). The shared
helper lives in `services/telegram_format.py::build(...)`. Existing S1
messages were rewritten to the same visual language.

### 1.3 `TelegramService.send_message()` — signature fix (bug)

`services/telegram_service.py` originally required both `title` **and**
`message`. That crashed every Strategy-2 call site (which pass a single
pre-formatted `build(...)` string as the title). Fixed by making
`message` optional with default `""`.

```python
def send_message(self, title: str, message: str = "", level: str = "INFO") -> bool:
Without this fix, the Strategy-2 tracker crashes on every refresh
and S2 never records a touch.

1.4 Simulation replay — attempted and removed
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

1.5 Daily MongoDB backup (new)
Every day at 16:00 IST (configurable) the pipeline zips data/
(configurable folder list) and stores the archive inline in MongoDB
under UPSTOX_APP_V2.backup_collection:

Doc _id: market_engine_v1_daily_data_backup

If zip ≤ 15 MB → status = "saved", data = BinData(...)

If zip > 15 MB → status = "skipped_size_limit", data = null,
reason = "Unable to save ... exceeds ... limit", note = "Backup archive NOT stored inline..."

Full metadata: archive_name, archive_size_bytes, archive_size_human,
file_count, updated_at, updated_at_utc, limit_bytes, limit_human,
max_blob_mb, run_reason

Full details in §6.

Files added for the daily backup:

text
services/daily_backup_service.py
token_tasks/daily_backup_task.py
Endpoints added:

text
GET  /admin/daily-backup-status
POST /admin/daily-backup-run
1.6 Change summary table
Change	Type	Files touched
Strategy 2 pipeline	New feature	7 files (services + routes + tasks)
Telegram message re-design + format helper	New helper	services/telegram_format.py + all S1/S2 task files + admin_routes.py + main.py
TelegramService.send_message() fix	Bug fix	services/telegram_service.py
Simulation replay (attempted, then removed)	Removed	5 files deleted, references reverted
Daily MongoDB backup	New feature	services/daily_backup_service.py, token_tasks/daily_backup_task.py, services/scheduler_service.py, routes/admin_routes.py, core/config.py, .env.example
2. Architecture overview
text
┌───────────────────────────────────────────────────────────────────────────┐
│                            Browser / UI                                   │
│  index.html (option chains) · chart.html · strategy_dashboard.html       │
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
│    strategy_routes        /admin/strategy/*   (Strategy 1)                │
│    strategy_2_routes      /admin/strategy2/*  (Strategy 2)                │
│    dashboard_routes       /dashboard, /ui/dashboard/strategy              │
│    upstox_routes          /api/upstox/*                                   │
│    log_routes             /logs, /ui/logs, /ui/logs/{filename}            │
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
│                                                                           │
│  Data / Registry                                                          │
│    services/backup_data_service.py     Mongo blob → data/ extraction      │
│    services/instrument_registry.py     scan data/ for ema_crosses.json    │
│    services/cleanup_service.py         delete transient folders           │
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
│  Strategy 1                                                               │
│    services/strategy_selection_service.py   Stage 1                       │
│    services/strategy_isolation_service.py   Stage 2                       │
│    services/strategy_live_tracker_service.py Stage 3 (R2 watcher)         │
│    services/strategy_order_service.py       Stage 4 (order)               │
│    services/strategy_meta.py                meta-test file helper         │
│                                                                           │
│  Strategy 2                                                               │
│    services/strategy_2_isolation_service.py    Stage 3 (S3 touch)         │
│    services/strategy_2_live_tracker_service.py Stage 3 watcher thread     │
│    services/strategy_2_order_service.py        Stage 4 (next-cross)       │
│                                                                           │
│  Daily backup                                                             │
│    services/daily_backup_service.py     zip folders → MongoDB             │
│                                                                           │
│  Scheduling                                                               │
│    services/scheduler_service.py        APScheduler singleton             │
└─────────────────────────────────────┬─────────────────────────────────────┘
                                      │
                                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                            External Systems                               │
│                                                                           │
│  MongoDB                                                                  │
│    <MONGO_DB>.<TOKENS_COLLECTION>               Upstox token              │
│    <MONGO_DB>.<BACKUP_METADATA_COLLECTION>      candle-backup metadata    │
│    <MONGO_DB>.<BACKUP_BLOB_COLLECTION>          candle-backup archive     │
│    UPSTOX_APP_V2.backup_collection              daily data backup         │
│                                                                           │
│  Upstox                                                                   │
│    REST: profile, search, historical, intraday                            │
│    WebSocket: MarketDataStreamerV3                                        │
│                                                                           │
│  Telegram                                                                 │
│    notifications + token management bot                                   │
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
  ├─ 10b. S1 Stage 1                        → data/strategy/stage_1_initial/
  ├─ 10c. S1 Stage 2                        → data/strategy/stage_2_isolation/
  ├─ 10d. S1 Stage 3 tracker.start()        → background thread
  ├─ 10e. S1 Stage 3 catch-up               → if past window
  ├─ 10f. S1 Stage 4 catch-up               → if past 09:18
  ├─ 10g. S2 Stage 3 tracker.start()        → background thread
  ├─ 10h. S2 Stage 3 snapshot               → reads stage_2/, filters to one contract
  ├─ 10i. S2 Stage 4                        → only if isolated exists
  ├─ 11.  live_feed_service.start/restart()
  ├─ 12.  scheduler_service.start()
  ├─ 13.  telegram_token_bot.start()
  ├─ 14.  Telegram completion
  └─ 15.  return result dict
main.py mounts every router and shuts down every background component
in the lifespan finally block.

3. Strategy 1 — Highly active day strategy
Best on days that move fast: news, gap opens, wide opening range,
sustained momentum. Four stages, all driven by the Opening Range and
a 9/21 EMA cross on each candidate option contract.

Stage	Trigger (IST)	Purpose	Output
Stage 1	09:16:30	Buffer filter around OR midpoint + Upstox history (30 days) + last-day-last-cross tail analysis	data/strategy/stage_1_initial/<TICKER>/<INDEX>/<strike>_<TYPE>/
Stage 2	09:17:00	Bullish-priority EMA-cross isolation, with CE/PE strike-level pairing	data/strategy/stage_2_isolation/<TICKER>/<INDEX>/<strike>_<TYPE>/
Stage 3	09:15 – 09:20 (continuous)	Per-contract R2 activation watcher	stage_3_summary.json, stage_3_decisions.json
Stage 4	09:18:00	Nearest-to-ATM per index → FIFO by R2 touch → paper order	stage_4_order/<TICKER>/<INDEX>/orders.json
Key rules:

Buffer — ATM ± typical_buffer_points (default NIFTY ±300, SENSEX ±900; configurable via STRATEGY_INDEXES_JSON).

Isolation — bullish priority rules: last-day bearish + today bullish → isolate; last-day bullish + today none → isolate; last-day none + today any → isolate; both bullish → isolate; bullish + today bearish → isolate; bearish-alone / bearish-both / both-none → NOT isolated.

Pairing — if either CE or PE isolates for a strike, both sides are copied to stage_2_isolation/. Trigger side gets decision="isolate", sibling gets decision="isolate_paired".

Stage 3 basis — per contract. Each contract is judged against its own 09:15–09:20 window high vs its own R2. The index R2 is informational context only.

Stage 4 entry LTP

Live run: freshest live-feed LTP.

Catch-up run: close of the option's own 1-min candle at or before 09:18, drawn from a tape sliced at the source to [09:15, 09:20]. No fallback to live LTP in catch-up.

Paper only — real Upstox order API not wired.

Endpoints — GET /admin/strategy, /admin/strategy/isolation, /admin/strategy/tracker/status, /admin/strategy/stage3, /admin/strategy/stage4 and per-ticker variants. POST /admin/strategy/recalculate, /admin/strategy/isolation/recalculate, /admin/strategy/order/run.

4. Strategy 2 — Normal day fallback strategy
Best on days when Strategy 1's conditions are not met: no clean R2
breakout, range-bound behaviour, low opening momentum. Rather than
forcing a trade off an R2 break that never happens, Strategy 2 looks
for a deeper pullback on the option premium (isolation level touch)
and waits for a fresh EMA cross on that same contract to confirm
direction before placing the order.

Strategy 2 reuses Stage 1 and Stage 2 of Strategy 1 unchanged.
Only Stage 3 and Stage 4 are replaced.

Stage	When	Purpose	Output
Stage 3 (S2)	09:15 – 15:30 (continuous background watcher)	Watch all Stage-2 isolated contracts, record the first touch of each contract's own isolation level (S3 default), filter to one winning contract	data/strategy2/stage_3_isolation_level/stage_3_summary.json + per-contract s3_touch.json
Stage 4 (S2)	On the next cross after the S3 touch	First EMA cross strictly after the S3 touch. Bullish → BUY same type. Bearish → BUY opposite type at the same timestamp. Paper order.	data/strategy2/stage_4_order/<TICKER>/<INDEX>/orders.json + order_meta_test.json
Isolation-level touch (Stage 3)
Isolation level is configurable — S1, S2, S3, R1, R2, R3 (default S3).

Each contract's own level is read from its own opening_range.json.

"Touch" = the contract's LTP reaches or crosses its own isolation level. Only the first touch per contract is kept.

Filter to one winner

If only one touched → that's the pick.
Otherwise, filter by ticker priority (default NIFTY before SENSEX, configurable via STRATEGY_2_TICKER_PRIORITY).
Within the winning ticker, pick the strike nearest to that index's ATM (|strike − index LTP| minimum).
If a contract's isolation level cannot be determined → silently skipped.

Next-cross order (Stage 4)
Reads the isolated pick from stage_3_summary.json.

Scans the contract's live 1-min EMA candles for the first cross whose minute is strictly greater than the S3-touch minute.

Bullish cross on CE → BUY same CE.

Bearish cross on CE → BUY opposite PE at the same strike.

(Symmetric for PE.)

Entry LTP — same-type order: cross candle close. Opposite-type order: sibling's own LTP at the same minute (best effort; falls back to cross close).

Paper order only.

Worked example (16 Oct 2026, NIFTY 22850 CE)
text
First 09:15 candle: H = 253.90   L = 205.05
    A   = 229.475
    D_H = D_L = 24.425
    S3  = L − 2·D_L = 156.20

Timeline:
  09:15  first candle
  09:27  low = 155.65 ≤ 156.20   → S3 TOUCHED
  09:53  bullish cross (valid)   → NEXT cross after touch
                                     → BUY 22850 CE at 09:53

Alternate:
  If 09:53 cross were BEARISH
    → BUY 22850 PE at the 09:53 timestamp
Known limitation
The Stage 3 watcher only sees live LTPs from the moment it starts.
If the process restarts mid-day after an S3 touch has already happened,
that touch is not retroactively detected (the in-memory touches dict is
cleared on restart). A historical back-scan is a future enhancement.

Endpoints
GET /admin/strategy2 — Stage 3 summary

GET /admin/strategy2/tracker/status

POST /admin/strategy2/tracker/start / stop

POST /admin/strategy2/stage3/run

GET /admin/strategy2/stage4

POST /admin/strategy2/stage4/run

5. Live data pipeline
text
Upstox WebSocket (MarketDataStreamerV3)
  │
  ▼
LiveFeedService._handle_feed(payload)
  │  for each instrument_key:
  ├─ _extract_tick(feed) → { ltp, prev_close, volume, candle_1m (interval "I1") }
  │
  ├─► _latest_ltp[key] = ltp    ← S1 Stage 3 tracker + S2 Stage 3 tracker poll this
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

6. Daily MongoDB backup
A new service that runs every day at 16:00 IST (configurable) and
zips a configurable list of folders (default ["data"]) into a single
archive. The archive is stored inline in MongoDB if it fits under
the configured limit, otherwise only metadata is saved.

Destination
Database: UPSTOX_APP_V2 (via DAILY_BACKUP_MONGO_DB)

Collection: backup_collection (via DAILY_BACKUP_COLLECTION)

Doc _id: market_engine_v1_daily_data_backup (via DAILY_BACKUP_DOC_ID)
— deliberately different from backup_historical_candles so it does not
collide with the candle-backup archive that is consumed on every refresh.

Behaviour matrix
Scenario	Archive size	DB doc status	DB doc contents
Normal (data/ zips ≤15 MB)	e.g. 8.4 MB	saved	data = BinData, reason: null, note: "Backup archive stored inline..."
Data grows large	e.g. 42 MB	skipped_size_limit	data: null, reason: "Unable to save the 'data.zip' archive to MongoDB inline because it exceeds the configured limit (42.0 MB > 15.0 MB)...", note: "Backup archive NOT stored inline..."
Feature disabled	—	—	{"status":"disabled"} returned, no doc written
Folders missing	0 B	—	{"status":"no_folders"} returned, no doc written
Manual concurrent run	—	—	{"status":"already_running"} returned, no doc written
MongoDB unreachable	—	—	{"status":"error", "error": "..."}, logged
Document schema (saved case)
javascript
{
  _id: 'market_engine_v1_daily_data_backup',
  archive_name: 'data.zip',
  archive_size_bytes: 8806042,
  archive_size_human: '8.4 MB',
  data: BinData(0, '...'),                           // the zip bytes
  folder: 'market-engine-v1',
  folders: [ 'data' ],
  skipped_folders: [],
  file_count: 3412,
  updated_at: '2026-10-18 16:00:14 IST',
  updated_at_utc: ISODate('2026-10-18T10:30:14.933Z'),
  limit_bytes: 15728640,
  limit_human: '15.0 MB',
  max_blob_mb: 15,
  note: 'Backup archive stored inline (8.4 MB). Doc _id=... database=... collection=...',
  reason: null,
  status: 'saved',
  run_reason: 'scheduled'
}
Document schema (skipped_size_limit case)
javascript
{
  _id: 'market_engine_v1_daily_data_backup',
  archive_name: 'data.zip',
  archive_size_bytes: 46123456,
  archive_size_human: '44.0 MB',
  data: null,                                        // no blob
  folder: 'market-engine-v1',
  folders: [ 'data' ],
  skipped_folders: [],
  file_count: 12804,
  updated_at: '2026-10-18 16:00:22 IST',
  updated_at_utc: ISODate('2026-10-18T10:30:22.114Z'),
  limit_bytes: 15728640,
  limit_human: '15.0 MB',
  max_blob_mb: 15,
  note: 'Backup archive NOT stored inline: final zip is 44.0 MB (limit 15.0 MB). Metadata-only record.',
  reason: "Unable to save the 'data.zip' archive to MongoDB inline because it exceeds the configured limit (44.0 MB > 15.0 MB). Only metadata has been written. To capture the actual archive, either raise DAILY_BACKUP_MAX_BLOB_MB, reduce DAILY_BACKUP_FOLDERS, or copy the folder manually before the next refresh deletes it.",
  status: 'skipped_size_limit',
  run_reason: 'scheduled'
}
Design notes
The zip is built in a temp file on disk, never fully in memory. RAM
usage stays low regardless of data/ size.

Files ending in .tmp are skipped during zipping — the app's atomic
write pattern (tmp.write_text() + tmp.replace(final)) guarantees
data/ files are complete whenever the zipper reads them.

A non-blocking lock prevents a scheduled run and a manual run from
overlapping.

Symlinked subdirectories are not followed.

Folder paths that escape the project root are refused.

The service never raises — always returns a summary dict.

Endpoints
text
GET  /admin/daily-backup-status       → last-run in-memory summary
POST /admin/daily-backup-run          → trigger now (returns summary dict)
Manual verification
bash
curl -X POST http://localhost:8010/admin/daily-backup-run
curl http://localhost:8010/admin/daily-backup-status

# Read back from MongoDB (data field excluded for brevity)
mongosh "mongodb+srv://..." --eval '
  db.getSiblingDB("UPSTOX_APP_V2")
    .backup_collection
    .findOne({_id: "market_engine_v1_daily_data_backup"},
             {data: 0})
'
Note on data/ being wiped by the morning refresh
The daily refresh at 09:00 IST calls cleanup_service.run() which deletes
data/ before re-extracting the candle-backup archive. The daily
MongoDB backup is therefore a point-in-time snapshot (yesterday's full
strategy output at 16:00). It is not a live mirror. If you need additional
folders preserved across the morning cleanup, add them to
CLEANUP_RESERVED_FOLDERS.

7. Scheduler jobs
services/scheduler_service.py::SchedulerService.start() is
idempotent — safe to call multiple times.

Job id	When	Function
token_lifecycle_check	every UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES (default 30)	check_upstox_token_validity
daily_refresh	DAILY_REFRESH_DAYS at DAILY_REFRESH_HOUR:MINUTE (default Mon–Fri 09:00)	run_daily_refresh → _run_full_refresh_sync("daily")
opening_range_calc	DAILY_REFRESH_DAYS at OPENING_RANGE_HOUR:MINUTE:SECOND (default 09:16:10)	run_opening_range_calculation
strategy_selection_calc	DAILY_REFRESH_DAYS at OR + 20s (default 09:16:30)	run_strategy_selection (S1 Stage 1)
strategy_isolation_calc	DAILY_REFRESH_DAYS at OR + 50s (default 09:17:00)	run_strategy_isolation (S1 Stage 2)
strategy_order_calc	DAILY_REFRESH_DAYS at STRATEGY_ORDER_HOUR:MINUTE (default 09:18:00)	run_strategy_order (S1 Stage 4)
strategy_2_stage3_isolation	DAILY_REFRESH_DAYS at 09:18:15	run_strategy_2_isolation
strategy_2_stage3_recheck	DAILY_REFRESH_DAYS, every 5 min, hours 9–15	run_strategy_2_isolation
strategy_2_stage4_order	DAILY_REFRESH_DAYS at 15:25:00	run_strategy_2_order
daily_backup	DAILY_BACKUP_DAYS at DAILY_BACKUP_HOUR:MINUTE (default every day 16:00)	run_daily_backup
8. Configuration reference
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
DAILY_REFRESH_HOUR	9	Daily refresh hour
DAILY_REFRESH_MINUTE	0	Daily refresh minute
DAILY_REFRESH_DAYS	mon,tue,wed,thu,fri	Cron day-of-week
STRATEGY_VERBOSE_LOG	false	When true, dump per-instrument decision detail
OPENING_RANGE_FILENAME	opening_range.json	Output filename
OPENING_RANGE_FIRST_MINUTE	09:15	First 1-min candle
OPENING_RANGE_HOUR	9	OR calc trigger hour
OPENING_RANGE_MINUTE	16	OR calc trigger minute
OPENING_RANGE_SECOND	10	OR calc trigger second
Strategy 1
Variable	Default	Purpose
STRATEGY_ENABLED	true	Master switch
STRATEGY_BASE_DIR	data/strategy	Output root
STRATEGY_STAGE_1_NAME	stage_1_initial	Subfolder
STRATEGY_STAGE_2_NAME	stage_2_isolation	Subfolder
STRATEGY_STAGE_3_NAME	stage_3_r2_activated	Subfolder
STRATEGY_STAGE_4_NAME	stage_4_order	Subfolder
STRATEGY_BUFFER_MODE	typical	typical or typical_plus_minus_avg
STRATEGY_INDEXES_JSON	—	JSON array override for index list
STRATEGY_FETCH_HISTORY	true	Resolve strikes + fetch history
STRATEGY_HISTORY_DAYS	30	History window (days)
STRATEGY_MAX_WORKERS	6	Parallel workers for history fetch
STRATEGY_ISOLATION_ENABLED	true	Enable Stage 2
STRATEGY_ISOLATION_LOOKBACK_MINUTES	30	Last-day tail window
STRATEGY_ISOLATION_SKIP_UNRESOLVED	true	Skip unresolved strikes
STRATEGY_TRACKER_ENABLED	true	Enable Stage 3 watcher
STRATEGY_TRACKER_WINDOW_START	09:15	Stage 3 window start
STRATEGY_TRACKER_WINDOW_END	09:17	Stage 3 window end
STRATEGY_TRACKER_POLL_SECONDS	1	Poll cadence
STRATEGY_ORDER_ENABLED	true	Enable Stage 4
STRATEGY_ORDER_NIFTY_PICKS	3	NIFTY keeps N nearest-ATM
STRATEGY_ORDER_SENSEX_PICKS	5	SENSEX keeps N nearest-ATM
STRATEGY_ORDER_FINAL_PICKS	1	Final picks after FIFO
STRATEGY_ORDER_HOUR	9	Stage 4 hour
STRATEGY_ORDER_MINUTE	18	Stage 4 minute
STRATEGY_ORDER_QTY	1	Paper order quantity
STRATEGY_TRACKER_INDEX_KEYS	NSE_INDEX|Nifty 50,BSE_INDEX|SENSEX	Index keys to watch
Strategy 2
Variable	Default	Purpose
STRATEGY_2_ENABLED	true	Master switch
STRATEGY_2_BASE_DIR	data/strategy2	Output root
STRATEGY_2_ISOLATION_LEVEL	S3	Watch level (S1/S2/S3/R1/R2/R3)
STRATEGY_2_TICKER_PRIORITY	NIFTY,SENSEX	Priority when multiple touch
STRATEGY_2_TRACKER_ENABLED	true	Enable S3-touch watcher
STRATEGY_2_TRACKER_WINDOW_START	09:15	Watcher active window start
STRATEGY_2_TRACKER_WINDOW_END	15:30	Watcher active window end
STRATEGY_2_TRACKER_POLL_SECONDS	1	LTP poll cadence
STRATEGY_2_ORDER_ENABLED	true	Enable Stage 4
STRATEGY_2_ORDER_QTY	1	Paper order quantity
STRATEGY_2_STAGE_3_NAME	stage_3_isolation_level	Subfolder
STRATEGY_2_STAGE_4_NAME	stage_4_order	Subfolder
Daily backup
Variable	Default	Purpose
DAILY_BACKUP_ENABLED	true	Master switch
DAILY_BACKUP_HOUR	16	Scheduler hour
DAILY_BACKUP_MINUTE	0	Scheduler minute
DAILY_BACKUP_DAYS	mon,tue,wed,thu,fri,sat,sun	Cron day-of-week
DAILY_BACKUP_FOLDERS	data	Folders to zip (relative to project root)
DAILY_BACKUP_MONGO_DB	UPSTOX_APP_V2	Destination database
DAILY_BACKUP_COLLECTION	backup_collection	Destination collection
DAILY_BACKUP_DOC_ID	market_engine_v1_daily_data_backup	Destination doc _id
DAILY_BACKUP_MAX_BLOB_MB	15	Inline blob limit; larger archives are metadata-only
DAILY_BACKUP_SOURCE_LABEL	project dir name	Value written to the folder field
9. Routes & API reference
Pages / UI
Method	Path	Purpose
GET	/	Multi-index dashboard + option chains
GET	/charts	Instrument search page
GET	/chart/{instrument_key}	Candlestick chart
GET	/dashboard	Strategy dashboard
GET	/isolated-dashboard	Redirect → /
GET	/favicon.ico	204
Logs
Method	Path	Purpose
GET	/logs	HTML log viewer
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
GET	/admin/daily-backup-status	Last daily-backup run summary
POST	/admin/daily-backup-run	Trigger the daily backup now
Admin — Opening Range
Method	Path	Purpose
GET	/admin/opening-range	All OR payloads
GET	/admin/opening-range/{instrument_key}	One instrument
POST	/admin/opening-range/recalculate	Force full recalc
POST	/admin/opening-range/recalculate/{instrument_key}	Force one
Admin — Strategy 1
Method	Path	Purpose
GET	/admin/strategy	Stage 1 summary
GET	/admin/strategy/indexes	Stage 1 per-index list
GET	/admin/strategy/{ticker}	Stage 1 per-ticker
GET	/admin/strategy/{ticker}/strikes	Stage 1 contracts
POST	/admin/strategy/recalculate	Force Stage 1
GET	/admin/strategy/isolation	Stage 2 summary
GET	/admin/strategy/isolation/indexes	Stage 2 per-index
GET	/admin/strategy/isolation/{ticker}	Stage 2 per-ticker
GET	/admin/strategy/isolation/{ticker}/strikes	Stage 2 contracts
POST	/admin/strategy/isolation/recalculate	Force Stage 2
GET	/admin/strategy/tracker/status	Stage 3 tracker status
POST	/admin/strategy/tracker/start	Start Stage 3 tracker
POST	/admin/strategy/tracker/stop	Stop Stage 3 tracker
GET	/admin/strategy/stage3	Stage 3 summary
GET	/admin/strategy/stage3/{ticker}	Stage 3 per-ticker
GET	/admin/strategy/stage4	Stage 4 summary
GET	/admin/strategy/stage4/{ticker}	Stage 4 per-ticker
POST	/admin/strategy/order/run	Force Stage 4
Admin — Strategy 2
Method	Path	Purpose
GET	/admin/strategy2	Stage 3 summary
GET	/admin/strategy2/tracker/status	S2 tracker status
POST	/admin/strategy2/tracker/start	Start S2 tracker
POST	/admin/strategy2/tracker/stop	Stop S2 tracker
POST	/admin/strategy2/stage3/run	Force Stage 3 run
GET	/admin/strategy2/stage4	Stage 4 summary
POST	/admin/strategy2/stage4/run	Force Stage 4 run
Dashboard data
Method	Path	Purpose
GET	/ui/dashboard/strategy	JSON payload (auto-refreshed every 5 s)
Folders
Method	Path	Purpose
GET	/admin/folders	List exposable folders
GET	/admin/folders/{folder_name}	One folder's metadata
GET	/admin/folders/{folder_name}/download	Stream folder as .zip
10. WebSocket reference
Path	Topic	Purpose
/ws/live	feed	Live tick snapshots, filtered
/ws/chart	feed	Chart feed snapshots
/all-feeds (alias /ws/all-feeds)	feed	All-instrument feed
/ws/ema-crosses	crosses	EMA cross events
/ws/ema	ema (poll)	Streaming EMA snapshot per instrument
Clients can send {"action":"subscribe","instrument_key":"..."} on
the feed sockets to add subscriptions on the fly.

11. Telegram commands & notifications
Bot commands (authorized chat only)
Command	Purpose
/save_token	Start token entry — next private message is validated and saved
/token_status	Masked token metadata
/token_check	Validate the stored token
/cancel	Cancel pending token entry
/help	List commands
The bot deletes the submitted token message, validates it against
Upstox before saving, and never echoes the raw value.

Notification style
All pipeline messages (startup, shutdown, stage start/complete/fail,
strategy order placed, daily backup, token lifecycle) are sent as
emoji + divider messages that render identically across Telegram
clients (no markdown). The shared helper is
services/telegram_format.build(...).

Example — Strategy 1 Stage 1 completion:

text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 STRATEGY 1 · STAGE 1 · COMPLETED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Mode:          scheduled
• Catch-up:      no
• Post-market:   no
• Scheduled:     09:16:30
• Actual:        09:16:32
• Avg buffer:    600.0
• Buffer mode:   typical
─────────────────────────────
🎯 Index results
   • Total: 2 · ✅ 2 · ⚠️ 0 · ❌ 0
   • ✅ NIFTY: 13 strikes · 26 contracts · 26 resolved
   • ✅ SENSEX: 19 strikes · 38 contracts · 38 resolved
─────────────────────────────
📈 Aggregate
   • Strikes: 32
   • Contracts: 64
   • Resolved: 64
✅ Status: SUCCESS — 2/2 indexes ok
12. Data layout
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
│
├── strategy/                    ← STRATEGY 1
│   ├── stage_1_initial/<TICKER>/<INDEX>/
│   │   ├── stage_1_summary.json
│   │   └── <strike>_<TYPE>/
│   │       ├── strategy_meta.json
│   │       ├── index_opening_range.json    (index's OR — audit copy)
│   │       ├── opening_range.json          (strike's OWN OR)
│   │       ├── candles.json
│   │       ├── ema_crosses.json
│   │       └── last_day_cross.json
│   ├── stage_2_isolation/<TICKER>/<INDEX>/
│   │   ├── stage_2_summary.json
│   │   ├── strategy_isolation.json
│   │   └── <strike>_<TYPE>/isolation_meta.json
│   ├── stage_3_r2_activated/
│   │   ├── stage_3_summary.json
│   │   ├── stage_3_decisions.json
│   │   └── <TICKER>/<INDEX>/<strike>_<TYPE>/r2_activation.json
│   └── stage_4_order/
│       ├── stage_4_summary.json
│       └── <TICKER>/<INDEX>/orders.json, order_meta_test.json
│
└── strategy2/                   ← STRATEGY 2
    ├── stage_3_isolation_level/
    │   ├── stage_3_summary.json
    │   └── <TICKER>/<INDEX>/<strike>_<TYPE>/s3_touch.json
    └── stage_4_order/
        ├── stage_4_summary.json
        └── <TICKER>/<INDEX>/orders.json, order_meta_test.json
Logs
logs/<module>.log — rotating by line count, max 1000 lines, 3 backups

logs_text/<module>.txt — append-only plain text mirror

Deliberately not cleaned by cleanup_service — the logger holds
open file handles into both directories at import time; removing them
mid-process breaks rotation on Linux and fails outright on Windows.

Meta backups
meta_data/ — preserved across all refreshes. update_project.py
writes a logs_<YYYYMMDD_HHMMSS>.zip into it before each git update.

13. Logging
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
strategy_live_tracker_service
strategy_2_live_tracker_service
strategy_2_isolation_service
strategy_2_order_service
daily_backup_service
token_monitor
token_store
upstox_token_client
telegram_token_bot
Set STRATEGY_VERBOSE_LOG=true in .env to switch Stage 1 / Stage 2 /
Stage 3 / Strategy 2 logs from "lifecycle only" to full per-instrument
decision dumps.

14. Install, run, verify
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

GET /api/upstox/token — returns {"available": true, "masked": "..."}.

GET /live/status — feed reports running: true after startup.

GET /admin/backup-status — last candle-backup extraction summary.

GET /admin/opening-range — OR levels present for at least the core indexes.

GET /admin/strategy — Stage 1 summary from the last run.

GET /admin/strategy2 — Strategy 2 Stage 3 summary from the last run.

GET /admin/strategy2/tracker/status — should report running: true
(after the send_message() fix).

POST /admin/hard-refresh — full pipeline re-runs; completion message arrives in Telegram.

POST /admin/daily-backup-run — returns the backup summary and writes
the doc to UPSTOX_APP_V2.backup_collection.

Telegram: /token_status reports masked metadata; /save_token accepts
a valid token and rejects invalid ones.

Regression tests (mock MongoDB, Upstox, Telegram):

powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
15. Known limitations & gotchas
TelegramService.send_message() signature must stay message: str = "". Every Strategy 2 task, the S2 tracker, and every nicely-formatted Strategy 1 message call it as send_message(build(...), level=...). If message becomes required again, S2 stops working and the S1 stage wrappers crash.

S2 mid-day restart loses in-memory touches. strategy_2_isolation_service._touches is an in-memory dict. It persists across refresh (the service object is not re-instantiated) but not across restart. If a contract touched S3 at 10:00 and the process restarts at 14:00 with the price recovered, S2 won't know the touch ever happened. Historical back-scan is a future enhancement.

Cleanup wipes data/ before backup extraction. Startup order is intentional: cleanup → token → backup → registry. Do not reorder unless you also change the cleanup policy.

Logs are deliberately not cleaned — the logger holds open handles into logs/ and logs_text/.

The daily backup is a point-in-time snapshot. data/ is wiped at 09:00 every weekday by cleanup, so the 16:00 backup captures the previous day's full strategy output. It is not a live mirror.

DAILY_BACKUP_MAX_BLOB_MB (15 MB) is a hard inline cap. Archives larger than that will be saved as metadata-only with status = "skipped_size_limit". If you see that status repeatedly, either raise the limit, trim DAILY_BACKUP_FOLDERS, or move the archive out of Mongo (e.g., MEGA / S3).

live_cross_store.reset_all() clears memory only. Disk live_crosses.json persists; the next save() hydrates from disk if the file's day matches the incoming cross.

Manual hard refresh passes loop=None. Startup has already wired the loop; the manual path reuses it. If you change this, pass asyncio.get_running_loop().

Port mismatch: Procfile = 8000, run.bat / start.sh = 8010.

Upstox SDK on_close signature. Use on_close(*args, **kwargs) — the historical on_close() (no args) crashes on close.

Paper orders only. The real Upstox /v2/order/place endpoint is not wired in strategy_order_service or strategy_2_order_service.

OR-then-Stage-1 race. On days when the 09:15 index candle is slow to finalize, opening_range_service.calculate_all() can complete with calculated=0 for the core indexes; Stage 1 then skips with or_unavailable:not_calculated. A retry-once guard (sleep 20 s, re-run OR, then Stage 1) is a known mitigation but not wired in this build.

STRATEGY_ENABLED=false disables Strategy 1. STRATEGY_2_ENABLED=false disables Strategy 2 independently — either or both can be off without affecting the rest of the app. DAILY_BACKUP_ENABLED=false disables the daily backup.

16. File inventory
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
│   ├── dashboard_routes.py                     strategy dashboard data
│   ├── folder_routes.py                        /admin/folders/*  (zip download)
│   ├── live_routes.py                          /live/*, /ws/*
│   ├── log_routes.py                           /logs, /ui/logs/*
│   ├── opening_range_routes.py                 /admin/opening-range/*
│   ├── page_routes.py                          /, /charts, /chart/{key}
│   ├── strategy_routes.py                      /admin/strategy/*     (S1)
│   ├── strategy_2_routes.py                    /admin/strategy2/*    (S2)
│   └── upstox_routes.py                        /api/upstox/*
├── services/
│   ├── backup_data_service.py                  candle-backup Mongo blob → data/
│   ├── cleanup_service.py                      transient folder cleanup
│   ├── daily_backup_service.py                 ★ daily MongoDB backup
│   ├── instrument_registry.py                  scan data/**/ema_crosses.json
│   ├── live_backfill_service.py                seed EMA engine
│   ├── live_broadcast.py                       in-process pub/sub
│   ├── live_cross_store.py                     live_crosses.json persistence
│   ├── live_ema_engine.py                      1-min candles + EMA cross
│   ├── live_feed_service.py                    Upstox WebSocket
│   ├── normalization.py                        selector normalization
│   ├── opening_range_service.py                OR levels + opening_range.json
│   ├── scheduler_service.py                    APScheduler singleton
│   ├── strategy_isolation_service.py           S1 Stage 2
│   ├── strategy_live_tracker_service.py        S1 Stage 3
│   ├── strategy_meta.py                        meta-test file helper
│   ├── strategy_order_service.py               S1 Stage 4
│   ├── strategy_selection_service.py           S1 Stage 1
│   ├── strategy_2_isolation_service.py         ★ S2 Stage 3
│   ├── strategy_2_live_tracker_service.py      ★ S2 Stage 3 watcher thread
│   ├── strategy_2_order_service.py             ★ S2 Stage 4
│   ├── telegram_format.py                      ★ emoji/divider formatter
│   ├── telegram_service.py                     Telegram HTTP client
│   └── token_service.py                        cached Upstox token
├── token_tasks/
│   ├── daily_backup_task.py                    ★ scheduler wrapper for daily backup
│   ├── daily_refresh.py                        scheduler wrapper for refresh
│   ├── opening_range_task.py                   scheduler wrapper for OR
│   ├── strategy_isolation_task.py              scheduler wrapper for S1 Stage 2
│   ├── strategy_order_task.py                  scheduler wrapper for S1 Stage 4
│   ├── strategy_selection_task.py              scheduler wrapper for S1 Stage 1
│   ├── strategy_2_isolation_task.py            ★ scheduler wrapper for S2 Stage 3
│   ├── strategy_2_order_task.py                ★ scheduler wrapper for S2 Stage 4
│   ├── telegram_token_bot.py                   Telegram long-polling bot
│   ├── token_monitor.py                        token validity checks
│   ├── token_store.py                          MongoDB token CRUD
│   └── upstox_token_client.py                  Upstox profile/funds API
├── templates/
│   ├── base.html                               shared nav + CSS
│   ├── chart.html                              candlestick + EMA + OR levels
│   ├── index.html                              multi-index dashboard + option chain
│   ├── instrument_list.html                    Upstox instrument search
│   ├── show_logs.html                          log viewer
│   └── strategy_dashboard.html                 strategy dashboard
├── logs/                                       rotating .log files
├── logs_text/                                  append-only .txt files
├── meta_data/                                  log backups (preserved across refresh)
└── data/                                       extracted backup + strategy output
★ = added since the initial Strategy-1-only release.

Reference docs
architecture.md — deeper component map, startup flow diagram, live data pipeline, scheduler jobs, future modification guide

strategy_1_explainer.txt — Strategy 1 end-to-end description

strategy_2_explainer.txt — Strategy 2 end-to-end description

.env.example — every supported variable with defaults

text

---

## What this README delivers

| Section | Content |
|---|---|
| **§1 What's new** | Highlights Strategy 2, Telegram redesign, `send_message()` bug fix, simulation removal, daily backup — with the exact files each touched |
| **§2 Architecture** | Full ASCII component map + the 15-step `_run_full_refresh_sync` flowchart |
| **§3 Strategy 1** | Stage table + key rules + entry LTP rules + paper-order disclaimer + endpoint list |
| **§4 Strategy 2** | Stage table + isolation-touch rules + next-cross rules + worked example + known limitation + endpoints |
| **§5 Live pipeline** | Tick → extract → cache → EMA engine → broadcast chain |
| **§6 Daily backup** | Behaviour matrix + both document schemas + design notes + verification commands + point-in-time caveat |
| **§7 Scheduler** | All 10 jobs with their default triggers |
| **§8 Config** | Every env variable, grouped by concern, with defaults |
| **§9 Routes** | Full reference (~50 endpoints) grouped by area |
| **§10 WebSockets** | 6 paths + client subscribe protocol |
| **§11 Telegram** | Bot commands + visual style + example rendered message |
| **§12 Data layout** | Full tree including both `strategy/` and `strategy2/` + logs + meta |
| **§13 Logging** | Handlers + important modules + verbose flag |
| **§14 Install / verify** | Setup + run helpers + 13-step verification checklist |
| **§15 Gotchas** | 13 documented limitations, most critical first (`send_message` signature is #1) |
| **§16 File inventory** | Every file with a one-line purpose; ★ marks new files |

The README assumes a new developer who has never seen the project — they can go from `git clone` → `.env` setup → verify in about 10 minutes, and can audit any part of the pipeline against the reference.